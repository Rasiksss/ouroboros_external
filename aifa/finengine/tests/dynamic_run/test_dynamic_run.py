"""Цикл гибридного планировщика (часть 4)."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

import pytest

from finengine.contracts import (
    DynamicArtifact,
    ExtractedMetric,
    GoalIR,
    NodeRun,
    ScalarFact,
    VerificationFinding,
)
from finengine.llm.budget import BudgetExceeded
from finengine.skills.case_runtime import PlanInfeasible
from finengine.skills.production_runtime import _dynamic_outcome
from finengine.skills.spec.atom_author import AtomAuthor
from finengine.skills.spec import dynamic_run as dynamic_run_module
from finengine.skills.spec.dynamic_run import (
    _shown_value,
    DynamicRun,
    MoveOutcome,
    RunLimits,
    ScenarioResult,
    describe_execution,
    load_case_atoms,
)
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA, AtomRequest, EphemeralAtom, to_atom_def
from finengine.skills.spec import sealed_room
from finengine.skills.spec.graph_executor import ExecutionResult, RunInputs
from finengine.skills.spec.graph_planner import AtomStep, DYNAMIC_PLANNER_VERSION
from finengine.skills.spec.target import InputSpec
from finengine.skills.spec.target import Target
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.contracts import NodeKey
from test_atom_author import BAD_CODE, Script
from test_download_atom import CSV, _csv_ok, _serve
from test_ephemeral_atoms import _metric
from test_ephemeral_in_graph import CASE, ENTITY, OWNER


def _download(name: str) -> EphemeralAtom:
    suffix = name.removeprefix("eph.")
    return EphemeralAtom(AtomRequest(
        name=name, purpose=f"данные {suffix}", output_type=f"dyn:{suffix}",
        output_schema=DOWNLOAD_SCHEMA, url=f"https://data.example.org/{suffix}.csv",
    ))


def test_plan_steps_keeps_independent_goals_and_uses_the_last_as_root(base) -> None:
    atoms = [_download("eph.first_data"), _download("eph.second_data")]
    runtime = base.with_atoms([to_atom_def(atom) for atom in atoms])
    steps = [
        AtomStep(atom="eph.first_data", output_type="dyn:first_data"),
        AtomStep(atom="eph.second_data", output_type="dyn:second_data"),
    ]

    planned = runtime.plan_steps(
        owner_id=OWNER, case_id=CASE, goal_ir=GoalIR(raw_query="два ряда", intent_kind="document_qa"),
        target=Target(output_type="dyn:second_data", entity_ref=ENTITY), steps=steps,
    )

    graph = planned.record.graph
    assert graph.root.atom_name == "eph.second_data"
    assert {node.node_key.atom_name for node in graph.nodes} == {"eph.first_data", "eph.second_data"}
    assert graph.planner_version == DYNAMIC_PLANNER_VERSION


def test_plan_steps_turns_identity_errors_into_plan_infeasible(base, monkeypatch) -> None:
    monkeypatch.setattr(
        base.planner, "plan_with_atoms",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("identity is empty")),
    )

    with pytest.raises(PlanInfeasible, match="identity is empty"):
        base.plan_steps(
            owner_id=OWNER, case_id=CASE,
            goal_ir=GoalIR(raw_query="ряд", intent_kind="document_qa"),
            target=Target(output_type="ExtractedMetric", entity_ref=ENTITY),
            steps=[AtomStep(atom="read.value", output_type="ExtractedMetric", concept="EBITDA")],
        )


CALC_SCHEMA = {
    "type": "object", "required": ["margin_pct"],
    "properties": {"margin_pct": {"type": "number", "x-unit": "%"}},
}
CALC_CODE = """
def run(inputs):
    by = {item["concept"]: item["scalar_fact"]["value"] for item in inputs["ExtractedMetric"]}
    return {"margin_pct": by["EBITDA"] / by["REVENUE"] * 100}
"""


def _atom_doc(request: AtomRequest) -> dict:
    return {
        "name": request.name, "purpose": request.purpose,
        "requires": [
            {"input_type": spec.input_type, "concept": spec.concept} for spec in request.requires
        ],
        "output_type": request.output_type, "output_schema": request.output_schema,
        **({"url": request.url} if request.url else {}),
    }


def _move(*, scenarios=(), atoms=(), steps=()) -> str:
    return json.dumps({
        "objective": "получить промежуточные данные", "reason": "они нужны для ответа",
        "action": "execute", "scenarios": list(scenarios),
        "new_atoms": [_atom_doc(atom) for atom in atoms], "steps": list(steps),
        "answer": "", "evidence_ids": [],
    }, ensure_ascii=False)


def _finalize(evidence_id: str) -> str:
    return json.dumps({
        "objective": "ответить", "reason": "результат готов", "action": "finalize",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": "Маржа рассчитана.",
        "evidence_ids": [evidence_id],
    }, ensure_ascii=False)


def _metric_for_period(period_ref: str, value: float) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="doc",
        concept="SALES_REVENUE", period_ref=period_ref, value=value, unit="RUB_MLN",
    )
    return ExtractedMetric(scalar_fact=fact, concept="SALES_REVENUE", period_ref=period_ref)


def _artifact_row(*metrics: ExtractedMetric) -> dict:
    return {
        "output_type": "ExtractedMetric", "concept": "SALES_REVENUE",
        "period_ref": None, "payloads": metrics,
        "evidence_ids": [metric.scalar_fact.evidence_id for metric in metrics],
        "dependency_evidence_ids": [],
    }


def _terminal_reply(*, action: str, answer: str, reason: str, evidence_ids=(), deliverables=()) -> str:
    return json.dumps({
        "objective": "завершить", "reason": reason, "action": action,
        "scenarios": [], "new_atoms": [], "steps": [], "answer": answer,
        "evidence_ids": list(evidence_ids), "deliverables": list(deliverables),
    }, ensure_ascii=False)


def _finalize_with_missing_file() -> str:
    """Ответ, который правила цикла не принимают: он называет файл, которого в `Case` нет."""
    return _terminal_reply(action="finalize", answer="Файл готов.", reason="итог", deliverables=("no-such-file",))


@dataclass
class ExtractScenario:
    runtime: object
    passport: object
    calls: int = 0

    def __call__(self, text: str) -> ScenarioResult:
        self.calls += 1
        for concept, value in (("EBITDA", 500.0), ("REVENUE", 2000.0)):
            metric = _metric(concept, value)
            self.runtime.store.put_artifact(
                OWNER, CASE,
                node_key=NodeKey(atom_name="read.value", atom_version="1.0.0",
                                 args_fingerprint=f"concept={concept}"),
                output_type="ExtractedMetric", payloads=(metric,), concept=concept,
                entity_ref=ENTITY, period_ref="2024", basis=None,
                document_version="sha256:v1", policy_version=self.runtime.policy.policy_version,
                passport=self.passport,
            )
        return ScenarioResult(True, "✓ extract_registered_metric: ExtractedMetric")


@dataclass
class DynamicScenario:
    runtime: object

    def __call__(self, text: str) -> ScenarioResult:
        artifact = DynamicArtifact(
            output_type="dyn:seed", payload={"value": 1}, origin=["external"], producer="seed@1",
        )
        self.runtime.store.put_artifact(
            OWNER, CASE, node_key=NodeKey(atom_name="seed", atom_version="1", args_fingerprint="seed"),
            output_type="dyn:seed", payloads=(artifact,), concept=None, entity_ref=ENTITY,
            period_ref=None, basis=None, document_version="sha256:v1",
            policy_version=self.runtime.policy.policy_version,
        )
        return ScenarioResult(True, "✓ dyn:seed")


class RefusingScenario:
    def __call__(self, text: str) -> ScenarioResult:
        return ScenarioResult(False, "✗ данных нет в отчёте")


@dataclass
class ServiceEvidenceScenario:
    calls: int = 0
    rows: list[dict] = field(default_factory=list)

    def __call__(self, text: str) -> ScenarioResult:
        self.calls += 1
        self.rows.extend([
            {
                "output_type": "DocumentText", "evidence_ids": [f"document-{self.calls}"],
                "payloads": [],
            },
            {
                "output_type": "RunPassport", "evidence_ids": [f"passport-{self.calls}"],
                "payloads": [],
            },
        ])
        return ScenarioResult(True, "✓ read.value EBITDA: результата нет (0)")


def test_three_move_run_executes_scenario_download_repairs_code_and_finalizes(
    base, local_room, monkeypatch,
) -> None:
    seen_http = _serve(monkeypatch, _csv_ok)
    download = AtomRequest(
        name="eph.brent_csv", purpose="ряд Brent", output_type="dyn:brent_csv",
        output_schema=DOWNLOAD_SCHEMA, url="https://data.example.org/brent.csv",
    )
    calc = AtomRequest(
        name="eph.margin_with_brent", purpose="маржа EBITDA и ряд Brent",
        output_type="dyn:margin_with_brent", output_schema=CALC_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"), InputSpec("ExtractedMetric", "REVENUE"),
                  InputSpec("dyn:brent_csv")),
    )
    final_id = EphemeralAtom(calc, CALC_CODE).version
    planner = Script([
        _move(scenarios=("Извлеки EBITDA и выручку",), atoms=(download,),
              steps=({"atom": download.name},)),
        _move(atoms=(calc,), steps=({"atom": download.name}, {"atom": calc.name})),
        _finalize(final_id),
    ])
    author = Script([
        json.dumps({"code": BAD_CODE, "example_inputs": {}}, ensure_ascii=False),
        json.dumps({"code": CALC_CODE, "example_inputs": {}}, ensure_ascii=False),
    ])
    goal = GoalIR(raw_query="сопоставь маржу с Brent", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id,
        classification=classification_from_cell("ifrs_nf_ru"),
    )
    scenario = ExtractScenario(base, passport)
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=goal,
        target=Target(output_type="dyn:margin_with_brent", entity_ref=ENTITY, period_ref="2024"),
        inputs=RunInputs(document_text="отчёт"), run_scenario=scenario,
        complete_fn=planner, author=AtomAuthor(author), run_passport=passport,
    )

    result = run.run(deterministic_error="нет производителя")

    assert result.status == "finalized" and result.answer == "Маржа рассчитана."
    assert len(result.journal) == 2 and scenario.calls == 1
    assert len(seen_http) == 1, "готовая загрузка во втором ходе не исполняется повторно"
    assert len(author.prompts) == 2 and "ZeroDivisionError" in author.prompts[1]
    assert "content_type=text/csv" in planner.prompts[1]
    assert f"длина текста={len(CSV)}" in planner.prompts[1]


def _run(
    base, replies, *, scenario=None, author=None, limits=RunLimits(), run_passport=None,
) -> tuple[DynamicRun, Script]:
    planner = Script(list(replies))
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="сложный вопрос", intent_kind="document_qa"),
        target=Target(output_type="QuoteEvidence", entity_ref=ENTITY), inputs=RunInputs(),
        run_scenario=scenario, complete_fn=planner, author=author, limits=limits,
        run_passport=run_passport,
    )
    return dynamic, planner


STOPPED_LINE = "Не удалось получить все запрошенные данные."


def _give_up(answer: str = "Данных недостаточно.") -> str:
    return json.dumps({
        "objective": "остановиться", "reason": "пути исчерпаны", "action": "give_up",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": answer, "evidence_ids": [],
    }, ensure_ascii=False)


@pytest.mark.parametrize("bad", ["не json", _move().replace("получить промежуточные данные", "")])
def test_invalid_planner_reply_becomes_feedback_and_the_cycle_continues(base, bad) -> None:
    dynamic, planner = _run(base, [bad, _give_up()])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert "Твой ответ не принят" in planner.prompts[1]


def test_repeated_rejected_planner_replies_use_the_last_word(base) -> None:
    dynamic, planner = _run(
        base, ["bad", "bad", "bad", _give_up()],
        limits=RunLimits(max_moves=12, idle_moves=3),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert result.answer == "Данных недостаточно."
    assert len(planner.prompts) == 4


def test_final_result_deduplicates_evidence_repeated_by_case_artifacts(base, monkeypatch) -> None:
    dynamic, _ = _run(base, [_give_up()])
    metric = _metric("REVENUE", 100.0)
    monkeypatch.setattr(dynamic, "_evidence", lambda evidence_id: [metric, metric])

    result = dynamic._result("finalized", "готово", (metric.scalar_fact.evidence_id,))

    assert result.evidence == (metric,)


def test_default_planner_requests_a_strict_nested_json_schema(monkeypatch) -> None:
    seen = {}

    async def _complete(prompt, **kwargs):
        seen.update(kwargs)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)

    asyncio.run(dynamic_run_module._default_complete("system", "prompt"))

    response_format = seen["response_format"]
    schema = response_format["json_schema"]["schema"]
    atom = schema["properties"]["new_atoms"]["items"]
    extra = schema["properties"]["steps"]["items"]["properties"]["extra"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert atom["properties"]["requires"]["type"] == "array"
    assert atom["properties"]["output_schema"]["type"] == "object"
    assert extra["additionalProperties"] is False
    assert set(extra["properties"]) == dynamic_run_module.ALLOWED_EXTRA_KEYS
    assert seen["retries"] == 1
    assert seen["max_output"] == 16000
    assert seen["reasoning_effort"] == "high"


def test_default_planner_does_not_inherit_the_long_extraction_timeout(monkeypatch) -> None:
    async def _complete(prompt, **kwargs):
        await asyncio.sleep(1)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(dynamic_run_module.settings, "planner_model_timeout_seconds", 0.01)

    with pytest.raises(TimeoutError):
        asyncio.run(dynamic_run_module._default_complete("system", "prompt"))


def test_the_planner_waits_by_its_own_limit_not_by_the_one_of_intent(monkeypatch) -> None:
    """Состояние цикла вырастает до 45–57 тыс. знаков (dev 08.10.2026), и ответ планировщика не укладывался
    в общие 120 с. Предел планировщика — своё поле: у разбора запроса он прежний."""
    async def _complete(prompt, **kwargs):
        await asyncio.sleep(0.2)
        return "{}"

    from finengine.config import Settings
    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(dynamic_run_module.settings, "routing_model_timeout_seconds", 0.01)
    monkeypatch.setattr(dynamic_run_module.settings, "planner_model_timeout_seconds", 5.0)

    assert asyncio.run(dynamic_run_module._default_complete("system", "prompt")) == "{}"
    fields = Settings.model_fields
    assert fields["planner_model_timeout_seconds"].default == 300.0
    assert fields["routing_model_timeout_seconds"].default == 120.0


def test_a_planner_timeout_is_logged_with_its_duration_and_the_prompt_size(monkeypatch, caplog) -> None:
    """Таймаут планировщика на dev не оставлял следа: в логе не было ни сколько ждали, ни какого размера
    было состояние — а от размера состояния время ответа и зависит."""
    async def _complete(prompt, **kwargs):
        await asyncio.sleep(1)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(dynamic_run_module.settings, "planner_model_timeout_seconds", 0.05)

    with caplog.at_level("WARNING", logger=dynamic_run_module.__name__), pytest.raises(TimeoutError):
        asyncio.run(dynamic_run_module._default_complete("s" * 700, "p" * 45_000))

    (record,) = [item for item in caplog.records if "планировщик" in item.getMessage()]
    message = record.getMessage()
    assert "45000" in message and "700" in message, message
    assert "предел 0.05 с" in message and re.search(r"за \d+\.\d с", message), message


def test_planner_gateway_failure_is_a_move_result_not_a_crash(base) -> None:
    dynamic, planner = _run(base, [ConnectionError("gateway down"), _give_up()])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert "модель-планировщик не ответила" in planner.prompts[1]


def test_budget_exceeded_from_the_planner_escapes_the_cycle(base) -> None:
    dynamic, _ = _run(base, [BudgetExceeded("runaway")])

    with pytest.raises(BudgetExceeded, match="runaway"):
        dynamic.run(deterministic_error="нет пути")


def test_move_limit_and_idle_limit_stop_the_cycle(base) -> None:
    move_limited, move_planner = _run(base, ["bad", "bad"], limits=RunLimits(max_moves=2))
    invalid_atom = AtomRequest(
        name="bad", purpose="x", output_type="not-dyn", output_schema={"type": "object"},
        url="https://data.example.org/a.csv",
    )
    idle_reply = _move(atoms=(invalid_atom,), steps=({"atom": "bad"},))
    idle_limited, idle_planner = _run(base, [idle_reply, idle_reply], limits=RunLimits(max_moves=4, idle_moves=2))

    by_moves = move_limited.run(deterministic_error="нет пути")
    by_idle = idle_limited.run(deterministic_error="нет пути")

    assert by_moves.status == "stopped" and by_idle.status == "stopped"
    assert STOPPED_LINE in by_moves.answer and STOPPED_LINE in by_idle.answer
    assert "лимит ходов" in move_planner.prompts[-1], "причина остановки — для планировщика, не для пользователя"
    assert "без новых результатов" in idle_planner.prompts[-1]


def test_idle_stop_asks_for_a_final_answer_with_evidence(base, monkeypatch) -> None:
    metric = _metric_for_period("6M 2026", 1453957.053)
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="read.value", atom_version="1.0.0", args_fingerprint="sales"),
        output_type="ExtractedMetric", payloads=(metric,), concept=metric.concept,
        entity_ref=ENTITY, period_ref=metric.period_ref, basis=None,
        document_version="sha256:v1", policy_version=base.policy.policy_version,
    )
    empty = _move(steps=({"atom": "read.value", "concept": "EBITDA"},))
    dynamic, planner = _run(
        base, [empty, _finalize(metric.scalar_fact.evidence_id)],
        limits=RunLimits(max_moves=3, idle_moves=1),
    )
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: read.value вернул пусто", "", empty_atoms=("read.value",),
        ),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "finalized" and result.evidence == (metric,)
    assert "исполнять новые действия нельзя" in planner.prompts[1].lower()


def test_rejected_last_word_falls_back_to_a_summary_with_partial_results(base, monkeypatch) -> None:
    scenario = ExtractScenario(base, None)
    empty = _move(steps=({"atom": "read.value", "concept": "EBITDA"},))
    dynamic, _ = _run(
        base,
        [_move(scenarios=("Извлеки выручку",)), empty, _finalize_with_missing_file()],
        scenario=scenario, limits=RunLimits(max_moves=4, idle_moves=1),
    )
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: read.value вернул пусто", "", empty_atoms=("read.value",),
        ),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped"
    assert result.evidence
    assert STOPPED_LINE in result.answer
    assert "REVENUE" in result.answer and "2 000" in result.answer


@pytest.mark.parametrize(
    ("replies", "limits"),
    [
        (["bad", _give_up("Остановлено после лимита ходов.")], RunLimits(max_moves=1, idle_moves=3)),
        (["bad", "bad", _give_up("Остановлено после серии отказов.")], RunLimits(max_moves=5, idle_moves=2)),
    ],
)
def test_move_exhaustion_and_rejection_series_use_the_last_word_path(base, replies, limits) -> None:
    dynamic, planner = _run(base, replies, limits=limits)

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert "исполнять новые действия нельзя" in planner.prompts[-1].lower()


def test_budget_exceeded_from_the_last_word_escapes_the_cycle(base, monkeypatch) -> None:
    empty = _move(steps=({"atom": "read.value", "concept": "EBITDA"},))
    dynamic, _ = _run(
        base, [empty, BudgetExceeded("last word runaway")],
        limits=RunLimits(max_moves=2, idle_moves=1),
    )
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: read.value вернул пусто", "", empty_atoms=("read.value",),
        ),
    )

    with pytest.raises(BudgetExceeded, match="last word runaway"):
        dynamic.run(deterministic_error="нет пути")


def test_give_up_uses_reason_and_drops_unknown_evidence_ids(base) -> None:
    metric = _metric_for_period("6M 2026", 1453957.053)
    base.store.put_artifact(
        OWNER, CASE, node_key=NodeKey(atom_name="read.value", atom_version="1.0.0", args_fingerprint="sales"),
        output_type="ExtractedMetric", payloads=(metric,), concept=metric.concept,
        entity_ref=ENTITY, period_ref=metric.period_ref, basis=None,
        document_version="sha256:v1", policy_version=base.policy.policy_version,
    )
    reply = _terminal_reply(
        action="give_up", answer="", reason="Для EBITDA данных нет.",
        evidence_ids=(metric.scalar_fact.evidence_id, "missing-id"),
    )
    dynamic, _ = _run(base, [reply])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and result.answer == "Для EBITDA данных нет."
    assert result.evidence == (metric,)


def test_give_up_keeps_accepted_partial_results_even_without_ids(base) -> None:
    passport = build_run_passport(
        "financial report", goal_ir_id="give-up-partials",
        classification=classification_from_cell("ifrs_nf_ru"),
    )
    dynamic, _ = _run(
        base, [_move(scenarios=("Извлеки EBITDA и выручку",)), _give_up("Для ответа не хватает данных.")],
        scenario=ExtractScenario(base, passport), run_passport=passport,
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert {item.concept for item in result.evidence} == {"EBITDA", "REVENUE"}
    events: list[dict] = []
    _dynamic_outcome(result, events.append, case_id=CASE, document_id="doc-1")
    assert {event["result"]["metric_id"] for event in events} == {
        item.metric_id for item in result.evidence
    }


def test_service_only_moves_count_as_idle_and_do_not_claim_new_results(base, monkeypatch) -> None:
    scenario = ServiceEvidenceScenario()
    move = _move(scenarios=("Прочитай EBITDA",))
    dynamic, _ = _run(
        base, [move, move, move], scenario=scenario,
        limits=RunLimits(max_moves=3, idle_moves=2),
    )
    monkeypatch.setattr(base.store, "list_artifacts", lambda *args: scenario.rows)

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped" and STOPPED_LINE in result.answer
    assert scenario.calls == 1
    assert all(entry.outcome.startswith("новых результатов нет") for entry in result.journal)


def test_succeeded_node_without_evidence_is_reported_as_empty() -> None:
    key = NodeKey(atom_name="read.value", atom_version="1.0.0", args_fingerprint="empty")
    node_run = NodeRun(
        node_key=key, run_id="run-empty", status="succeeded",
        sandbox_profile="document_read",
    )

    outcome = describe_execution(ExecutionResult({key: node_run}, {key: ()}, "succeeded"))

    assert outcome.headline == "результатов нет: read.value вернули пусто"
    assert outcome.details.startswith("∅ read.value")
    assert "нет результата (0)" in outcome.details


def test_refused_scenario_is_visible_in_the_move_headline(base) -> None:
    dynamic, _ = _run(
        base, [_move(scenarios=("Извлеки EBITDA",)), _give_up()],
        scenario=RefusingScenario(),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.journal[0].outcome.startswith("ход завершён с ошибками")


def test_scenario_only_move_and_refusal_are_returned_to_the_planner(base) -> None:
    scenario = ExtractScenario(base, None)
    planner = Script([
        _move(scenarios=("Извлеки показатели",), steps=()),
        _give_up(),
    ])
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE,
        goal=GoalIR(raw_query="вопрос", intent_kind="document_qa"),
        target=Target(output_type="QuoteEvidence", entity_ref=ENTITY), inputs=RunInputs(),
        run_scenario=scenario, complete_fn=planner, limits=RunLimits(idle_moves=3),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and scenario.calls == 1
    assert "extract_registered_metric" in planner.prompts[1]


def test_scenario_refusal_is_returned_to_the_planner(base) -> None:
    dynamic, planner = _run(
        base, [_move(scenarios=("Извлеки показатель",), steps=()), _give_up()],
        scenario=RefusingScenario(),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and "✗ данных нет" in planner.prompts[1]


def test_journal_names_executed_scenarios_and_atoms_with_their_outcomes(base, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    download = _download("eph.journal_data").request
    dynamic, _ = _run(
        base,
        [
            _move(
                scenarios=("Извлеки EBITDA",), atoms=(download,),
                steps=({"atom": download.name},),
            ),
            _give_up(),
        ],
        scenario=RefusingScenario(),
    )

    result = dynamic.run(deterministic_error="нет пути")

    outcome = result.journal[0].outcome
    assert "сценарий «Извлеки EBITDA»" in outcome and "✗ данных нет" in outcome
    assert "eph.journal_data" in outcome and "dyn:journal_data" in outcome


@dataclass
class SequenceAuthor:
    replies: list
    attempts: int = 3
    calls: list | None = None

    def __post_init__(self) -> None:
        self.calls = []

    def write(self, request, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def test_station_fault_is_feedback_and_does_not_create_an_atom_or_failure(base, monkeypatch) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )
    author_script = Script([json.dumps({"code": CALC_CODE, "example_inputs": {}})])
    monkeypatch.setattr(
        sealed_room, "run_code",
        lambda code, inputs, **kwargs: sealed_room.CodeRun(None, "timeout", station_fault=True),
    )
    scenario = ExtractScenario(base, None)
    dynamic, planner = _run(
        base,
        [_move(scenarios=("Извлеки",), atoms=(request,), steps=({"atom": request.name},)), _give_up()],
        scenario=scenario, author=AtomAuthor(author_script),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up"
    assert "комната станции недоступна" in planner.prompts[1]
    assert request.name not in dynamic.atoms and request.name not in dynamic.failures


def test_missing_same_move_input_runs_the_download_then_writes_code_next_move(
    base, local_room, monkeypatch,
) -> None:
    seen_http = _serve(monkeypatch, _csv_ok)
    download = AtomRequest(
        name="eph.source_csv", purpose="источник", output_type="dyn:source_csv",
        output_schema=DOWNLOAD_SCHEMA, url="https://data.example.org/source.csv",
    )
    calc = AtomRequest(
        name="eph.parse_source", purpose="разобрать", output_type="dyn:parsed_source",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:source_csv"),),
    )
    made = EphemeralAtom(request=calc, code="def run(inputs):\n    return {'margin_pct': 1.0}\n")
    author = SequenceAuthor([(made, None)])
    dynamic, planner = _run(
        base,
        [
            _move(atoms=(download, calc), steps=({"atom": download.name}, {"atom": calc.name})),
            _move(atoms=(calc,), steps=({"atom": calc.name},)),
            _give_up(),
        ],
        author=author,
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and len(author.calls) == 1
    assert dynamic.created == 2 and calc.name in dynamic.atoms
    assert len(seen_http) == 1
    assert "входов атома" in planner.prompts[1]


def _created_atoms_block(prompt: str) -> str:
    return prompt.split("## Созданные атомы этого разговора")[1].split("## Полученные артефакты")[0]


def test_state_marks_a_created_atom_that_has_not_produced_an_artifact_yet(base) -> None:
    dynamic, _ = _run(base, [])
    dynamic.atoms["eph.brent"] = _download("eph.brent")

    block = _created_atoms_block(dynamic_run_module.render_state(dynamic, "fb"))

    assert "eph.brent" in block and "НЕ ЗАПУЩЕН" in block


def test_state_has_one_line_with_value_for_each_period_evidence(base, monkeypatch) -> None:
    metrics = (
        _metric_for_period("6M 2026", 1453957.053),
        _metric_for_period("6M 2025", 1325073.447),
    )
    dynamic, _ = _run(base, [])
    monkeypatch.setattr(base.store, "list_artifacts", lambda *args: [_artifact_row(*metrics)])

    state = dynamic_run_module.render_state(dynamic, "fb")
    block = state.split("## Полученные артефакты\n", 1)[1].split("\n\n## Журнал ходов", 1)[0]
    lines = block.splitlines()[1:]

    assert len(lines) == 2
    for metric, period, value in zip(metrics, ("6M 2026", "6M 2025"), ("1453957.053", "1325073.447")):
        line = next(line for line in lines if metric.scalar_fact.evidence_id in line)
        assert "ExtractedMetric" in line and "SALES_REVENUE" in line
        assert period in line and value in line


def test_state_size_for_twenty_evidence_is_bounded(base, monkeypatch) -> None:
    metrics = tuple(_metric_for_period(f"P{index:02d}", 1000.0 + index) for index in range(20))
    dynamic, _ = _run(base, [])
    monkeypatch.setattr(base.store, "list_artifacts", lambda *args: [_artifact_row(*metrics)])

    state = dynamic_run_module.render_state(dynamic, "fb")

    assert state.count(" | ExtractedMetric | SALES_REVENUE | P") == 20
    assert len(state) < 36000


def test_state_does_not_mark_an_atom_after_its_step_ran(base, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    download = _download("eph.brent").request
    dynamic, planner = _run(base, [_move(atoms=(download,), steps=({"atom": download.name},)), _give_up()])

    dynamic.run(deterministic_error="нет пути")

    block = _created_atoms_block(planner.prompts[1])
    assert "eph.brent" in block and "НЕ ЗАПУЩЕН" not in block


def test_atom_limit_rejects_only_new_atoms_and_saved_sources_load_again(base) -> None:
    first = _download("eph.first_data")
    second = _download("eph.second_data")
    dynamic, _ = _run(base, [_give_up()], limits=RunLimits(max_new_atoms=1))

    assert dynamic._ensure_atom(first.request) is None
    assert dynamic._ensure_atom(first.request) is None
    assert "лимит" in dynamic._ensure_atom(second.request)
    assert load_case_atoms(base.store, OWNER, CASE)[first.request.name] == first


def test_healthy_equal_code_atom_is_reused_and_download_never_calls_the_author(base) -> None:
    request = AtomRequest(
        name="eph.parse_source", purpose="разобрать", output_type="dyn:parsed_source",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:source_csv"),),
    )
    known = EphemeralAtom(request, "def run(inputs):\n    return {'margin_pct': 1.0}\n")
    author = SequenceAuthor([])
    dynamic, _ = _run(base, [_give_up()], author=author)
    dynamic.atoms[request.name] = known

    assert dynamic._ensure_atom(request) is None
    assert dynamic._ensure_atom(_download("eph.source_csv").request) is None
    assert author.calls == []


def test_run_called_from_a_worker_thread_completes(base) -> None:
    import asyncio

    dynamic, _ = _run(base, [_give_up()])

    result = asyncio.run(asyncio.to_thread(dynamic.run, deterministic_error="нет пути"))

    assert result.status == "gave_up"


@pytest.mark.parametrize(
    ("failure", "feedback"),
    [(PlanInfeasible("нет производителя"), "план не собрался"),
     (TimeoutError("deadline"), "ход прерван по времени")],
)
def test_plan_and_execution_failures_become_feedback(base, monkeypatch, failure, feedback) -> None:
    monkeypatch.setattr(base, "plan_steps", lambda **kwargs: (_ for _ in ()).throw(failure))
    dynamic, planner = _run(
        base,
        [_move(steps=({"atom": "read.value", "concept": "EBITDA"},)), _give_up()],
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and feedback in planner.prompts[1]


def test_graph_failure_is_sent_with_previous_code_when_the_same_atom_is_repaired(base, local_room) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    bad = EphemeralAtom(request, "def run(inputs):\n    raise ValueError('graph boom')\n")
    good = EphemeralAtom(request, "def run(inputs):\n    return {'margin_pct': 1.0}\n")
    author = SequenceAuthor([(bad, None), (good, None)])
    scenario = DynamicScenario(base)
    dynamic, planner = _run(
        base,
        [
            _move(scenarios=("Извлеки",), atoms=(request,), steps=({"atom": request.name},)),
            _move(atoms=(request,), steps=({"atom": request.name},)),
            _give_up(),
        ],
        scenario=scenario, author=author,
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and len(author.calls) == 2, (
        dynamic.failures, dynamic.journal, planner.prompts,
    )
    assert "graph boom" in author.calls[1]["error"]
    assert author.calls[1]["previous_code"] == bad.code
    assert request.name not in dynamic.failures


def test_author_failure_does_not_erase_the_graph_error_for_the_next_attempt(base, monkeypatch) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )
    known = EphemeralAtom(request, "def run(inputs):\n    raise ValueError('boom')\n")
    author = SequenceAuthor([(None, "still bad"), (known, None)])
    dynamic, _ = _run(base, [_give_up()], author=author)
    dynamic.atoms[request.name] = known
    dynamic.failures[request.name] = "graph error"
    monkeypatch.setattr(dynamic_run_module, "case_inputs", lambda *args, **kwargs: {"ExtractedMetric": [{}]})

    assert "автор не написал" in dynamic._ensure_atom(request)
    assert dynamic._ensure_atom(request) is None

    assert [call["error"] for call in author.calls] == ["graph error", "graph error"]


def test_current_atoms_are_passed_to_case_inputs(base, monkeypatch) -> None:
    source = _download("eph.source_csv")
    request = AtomRequest(
        name="eph.parse_source", purpose="разобрать", output_type="dyn:parsed_source",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:source_csv"),),
    )
    made = EphemeralAtom(request, "def run(inputs):\n    return {'margin_pct': 1.0}\n")
    author = SequenceAuthor([(made, None)])
    dynamic, _ = _run(base, [_give_up()], author=author)
    dynamic.atoms[source.request.name] = source
    seen = []
    monkeypatch.setattr(
        dynamic_run_module, "case_inputs",
        lambda *args, **kwargs: seen.append(kwargs["atoms"]) or {"dyn:source_csv": [{}]},
    )

    assert dynamic._ensure_atom(request) is None
    assert seen == [dynamic.atoms]


def test_budget_exceeded_from_the_author_escapes_the_cycle(base) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("ExtractedMetric", "EBITDA"),),
    )
    scenario = ExtractScenario(base, None)
    author = AtomAuthor(Script([BudgetExceeded("author runaway")]))
    dynamic, _ = _run(
        base,
        [_move(scenarios=("Извлеки",), atoms=(request,), steps=({"atom": request.name},))],
        scenario=scenario, author=author,
    )

    with pytest.raises(BudgetExceeded, match="author runaway"):
        dynamic.run(deterministic_error="нет пути")


def test_problems_of_every_rejected_atom_reach_the_planner_by_name(base) -> None:
    bad = [
        AtomRequest(name=f"eph.bad_{index}", purpose="x", output_type="not-dyn",
                    output_schema={"type": "object"}, url=f"https://data.example.org/{index}.csv")
        for index in range(2)
    ]
    dynamic, planner = _run(base, [
        _move(atoms=bad, steps=({"atom": bad[0].name}, {"atom": bad[1].name})), _give_up(),
    ])

    dynamic.run(deterministic_error="нет пути")

    assert "eph.bad_0" in planner.prompts[1] and "eph.bad_1" in planner.prompts[1]


def test_repairs_that_only_rewrite_code_do_not_count_as_progress(base, local_room) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    versions = [
        EphemeralAtom(request, f"def run(inputs):\n    raise ValueError('boom {index}')\n")
        for index in range(4)
    ]
    author = SequenceAuthor([(atom, None) for atom in versions])
    repair = _move(atoms=(request,), steps=({"atom": request.name},))
    dynamic, _ = _run(
        base, [_move(scenarios=("Извлеки",), atoms=(request,), steps=({"atom": request.name},)),
               repair, repair, repair],
        scenario=DynamicScenario(base), author=author, limits=RunLimits(max_moves=4, idle_moves=2),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped" and STOPPED_LINE in result.answer
    assert len(author.calls) == 3


def test_a_repeated_move_does_not_report_reused_artifacts_as_new(base, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    download = _download("eph.a_data").request
    same_move = _move(atoms=(download,), steps=({"atom": download.name},))
    dynamic, planner = _run(base, [same_move, same_move, _give_up()], limits=RunLimits(idle_moves=5))

    result = dynamic.run(deterministic_error="нет пути")

    assert result.journal[0].outcome.startswith("✓ 1 новых результатов\n✓ eph.a_data")
    assert result.journal[1].outcome.startswith("новых результатов нет, всё уже было получено")
    assert "уже было" in planner.prompts[2]


def test_exact_empty_step_is_rejected_until_new_target_evidence_appears(base, monkeypatch) -> None:
    same = _move(steps=({"atom": "read.value", "concept": "EBITDA", "period_ref": "2024"},))
    events = []
    dynamic, _ = _run(
        base,
        [same, same, _move(scenarios=("Извлеки выручку",)), same, _give_up()],
        scenario=DynamicScenario(base), limits=RunLimits(max_moves=6, idle_moves=5),
    )
    dynamic.event_sink = events.append
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: read.value вернул пусто", "", empty_atoms=("read.value",),
        ),
    )

    result = dynamic.run(deterministic_error="нет пути")

    step_starts = [
        event for event in events
        if event["type"] == "dynamic_move" and event["status"] == "running" and event["steps"]
    ]
    rejected = [event for event in events if event["type"] == "dynamic_move" and event["status"] == "rejected"]
    assert result.status == "gave_up" and len(step_starts) == 2
    assert len(rejected) == 1 and "точно повторяет тупиковое действие" in rejected[0]["details"]


def test_same_refused_scenario_is_rejected(base) -> None:
    same = _move(scenarios=("Извлеки EBITDA по всем периодам",))
    events = []
    dynamic, _ = _run(
        base, [same, same, _give_up()], scenario=RefusingScenario(),
        limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.event_sink = events.append

    result = dynamic.run(deterministic_error="нет пути")

    scenario_starts = [
        event for event in events
        if event["type"] == "dynamic_move" and event["status"] == "running" and event["scenarios"]
    ]
    rejected = [event for event in events if event["type"] == "dynamic_move" and event["status"] == "rejected"]
    assert result.status == "gave_up" and len(scenario_starts) == 1
    assert len(rejected) == 1 and "точно повторяет тупиковое действие" in rejected[0]["details"]


def test_step_next_to_an_invalid_one_is_not_a_dead_end(base, monkeypatch) -> None:
    """dev 07.10.2026: мусорный шаг отклонил весь набор, и верный шаг созданного атома стал «тупиком»."""
    _serve(monkeypatch, _csv_ok)
    download = _download("eph.a_data").request
    events = []
    dynamic, _ = _run(
        base,
        [
            _move(atoms=(download,), steps=({"atom": "}],"}, {"atom": download.name})),
            _move(atoms=(download,), steps=({"atom": download.name},)),
            _give_up(),
        ],
        limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.event_sink = events.append

    result = dynamic.run(deterministic_error="нет пути")

    assert all(event.get("status") != "rejected" for event in events)
    assert "шаги отклонены" in result.journal[0].outcome
    assert result.journal[1].outcome.startswith("✓ 1 новых результатов")


def test_rewritten_ephemeral_atom_allows_the_same_step_again(base, monkeypatch) -> None:
    first = _download("eph.source_csv").request
    rewritten = AtomRequest(
        name=first.name, purpose="другой источник и способ", output_type=first.output_type,
        output_schema=first.output_schema, url="https://data.example.org/repaired.csv",
    )
    dynamic, _ = _run(
        base,
        [
            _move(atoms=(first,), steps=({"atom": first.name},)),
            _move(atoms=(rewritten,), steps=({"atom": rewritten.name},)),
            _give_up(),
        ],
        limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.atoms[first.name] = EphemeralAtom(first)
    events = []
    dynamic.event_sink = events.append
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: eph.source_csv вернул пусто", "", empty_atoms=(first.name,),
        ),
    )

    result = dynamic.run(deterministic_error="нет пути")

    starts = [event for event in events if event["type"] == "dynamic_move" and event["status"] == "running"]
    assert result.status == "gave_up" and len(starts) == 2
    assert all(event.get("status") != "rejected" for event in events)


def test_exact_step_can_retry_after_a_timeout(base, monkeypatch) -> None:
    same = _move(steps=({"atom": "read.value", "concept": "EBITDA"},))
    monkeypatch.setattr(base, "plan_steps", lambda **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")))
    events = []
    dynamic, _ = _run(base, [same, same, _give_up()], limits=RunLimits(max_moves=4, idle_moves=3))
    dynamic.event_sink = events.append

    result = dynamic.run(deterministic_error="нет пути")

    starts = [event for event in events if event["type"] == "dynamic_move" and event["status"] == "running"]
    assert result.status == "gave_up" and len(starts) == 2
    assert all(event.get("status") != "rejected" for event in events)


def test_exact_ephemeral_step_can_retry_after_a_station_fault(base, monkeypatch) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    DynamicScenario(base)("seed")
    dynamic, _ = _run(
        base,
        [
            _move(steps=({"atom": request.name},)),
            _move(steps=({"atom": request.name},)),
            _give_up(),
        ],
        limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.atoms[request.name] = EphemeralAtom(
        request, "def run(inputs):\n    return {'margin_pct': 1.0}\n",
    )
    monkeypatch.setattr(
        sealed_room, "run_code",
        lambda code, inputs, **kwargs: sealed_room.CodeRun(None, "timeout", station_fault=True),
    )
    events = []
    dynamic.event_sink = events.append

    result = dynamic.run(deterministic_error="нет пути")

    starts = [event for event in events if event["type"] == "dynamic_move" and event["status"] == "running"]
    assert result.status == "gave_up" and len(starts) == 2
    assert all(event.get("status") != "rejected" for event in events)


def test_station_fault_inside_the_graph_is_not_remembered_as_a_code_failure(base, monkeypatch) -> None:
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    healthy = EphemeralAtom(request, "def run(inputs):\n    return {'margin_pct': 1.0}\n")
    monkeypatch.setattr(
        sealed_room, "run_code",
        lambda code, inputs, **kwargs: sealed_room.CodeRun(None, "timeout", station_fault=True),
    )
    author = SequenceAuthor([])
    dynamic, planner = _run(
        base,
        [_move(scenarios=("Извлеки",), steps=({"atom": request.name},)), _give_up()],
        scenario=DynamicScenario(base), author=author,
    )
    dynamic.atoms[request.name] = healthy

    dynamic.run(deterministic_error="нет пути")

    assert dynamic.failures == {}, "сбой станции — не ошибка кода: автору чинить нечего"
    assert "комната станции недоступна" in planner.prompts[1]


# ---------------------------------------------------------------------------
# Ревью части 8
def _rejections(events: list) -> list[dict]:
    return [event for event in events if event["type"] == "dynamic_move" and event["status"] == "rejected"]


def test_download_with_a_rejected_contract_can_be_resent_with_a_valid_address(base) -> None:
    broken = AtomRequest(
        name="eph.brent", purpose="Brent", output_type="dyn:brent",
        output_schema=DOWNLOAD_SCHEMA, url="ftp://data.example.org/brent.csv",
    )
    fixed = AtomRequest(
        name="eph.brent", purpose="Brent", output_type="dyn:brent",
        output_schema=DOWNLOAD_SCHEMA, url="https://data.example.org/brent.csv",
    )
    events = []
    dynamic, _ = _run(
        base,
        [
            _move(atoms=(broken,), steps=({"atom": broken.name},)),
            _move(atoms=(fixed,), steps=({"atom": fixed.name},)),
            _give_up(),
        ],
        limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    assert _rejections(events) == [], "шаг не исполнялся: атома не было, повтором это не считается"


def test_code_atom_the_author_failed_to_write_can_be_resent_with_a_clearer_purpose(base) -> None:
    DynamicScenario(base)("seed")
    request = AtomRequest(
        name="eph.metric_calc", purpose="расчёт", output_type="dyn:metric_calc",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    clearer = AtomRequest(
        name=request.name, purpose="расчёт, уточнён способ", output_type=request.output_type,
        output_schema=CALC_SCHEMA, requires=request.requires,
    )
    author = SequenceAuthor([(None, "код не прошёл проверку"), (None, "код снова не прошёл")])
    events = []
    dynamic, _ = _run(
        base,
        [
            _move(atoms=(request,), steps=({"atom": request.name},)),
            _move(atoms=(clearer,), steps=({"atom": request.name},)),
            _give_up(),
        ],
        author=author, limits=RunLimits(max_moves=4, idle_moves=3),
    )
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    assert _rejections(events) == []
    assert len(author.calls) == 2, "автор должен получить уточнённое описание"


@dataclass
class FailedCheckScenario:
    runtime: object

    def __call__(self, text: str) -> ScenarioResult:
        metric = _metric_for_period("6M 2026", 1453957.053)
        finding = VerificationFinding(
            produced_by_atom="verify.bounds", produced_by_atom_version="1.0.0",
            source_ref=metric.scalar_fact.evidence_id, concept=metric.concept,
            check_kind="bounds", status="fail", detail="значение вне допустимых границ",
        )
        for output_type, payload, atom in (
            ("ExtractedMetric", metric, "read.value"), ("VerificationFinding", finding, "verify.bounds"),
        ):
            self.runtime.store.put_artifact(
                OWNER, CASE, node_key=NodeKey(atom_name=atom, atom_version="1.0.0", args_fingerprint="s"),
                output_type=output_type, payloads=(payload,), concept=metric.concept, entity_ref=ENTITY,
                period_ref=metric.period_ref, basis=None, document_version="sha256:v1",
                policy_version=self.runtime.policy.policy_version,
                dependency_evidence_ids=(metric.scalar_fact.evidence_id,) if atom == "verify.bounds" else (),
            )
        return ScenarioResult(True, "✓ extract")


@dataclass
class GiveUpNamingTheArtifact:
    """Планировщик: первым ходом сценарий, затем give_up с id значения из таблицы артефактов."""

    prompts: list[str] = field(default_factory=list)

    async def __call__(self, system: str, prompt: str) -> str:  # noqa: ARG002
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return _move(scenarios=("Извлеки выручку",))
        evidence_id = next(
            line.split("|", 1)[0].strip() for line in prompt.splitlines() if "| ExtractedMetric |" in line
        )
        reply = json.loads(_give_up("Проверенное значение получить не удалось."))
        reply["evidence_ids"] = [evidence_id]
        return json.dumps(reply, ensure_ascii=False)


def test_accepted_last_word_is_a_trace_event_of_the_next_move(base) -> None:
    events = []
    dynamic, _ = _run(base, ["bad", "bad", "bad", _give_up()], limits=RunLimits(max_moves=12, idle_moves=3))
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    last = events[-1]
    assert (last["type"], last["status"], last["action"], last["move"]) == ("dynamic_move", "done", "give_up", 4)
    assert last["details"] == "Данных недостаточно."


def test_rejected_last_word_leaves_its_reason_in_the_trace(base) -> None:
    events = []
    dynamic, _ = _run(
        base, ["bad", "bad", "bad", _finalize_with_missing_file()], limits=RunLimits(max_moves=12, idle_moves=3),
    )
    dynamic.event_sink = events.append

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped"
    last = events[-1]
    assert (last["status"], last["move"]) == ("rejected", 4)
    assert "несуществующие файлы" in last["details"]


@pytest.mark.parametrize("variant", [{"concept": " ebitda "}, {"period_ref": "6m  2026"}])
def test_cosmetic_changes_do_not_bypass_the_repeat_ban(base, monkeypatch, variant) -> None:
    first = {"atom": "read.value", "concept": "EBITDA", "period_ref": "6M 2026"}
    events = []
    dynamic, _ = _run(
        base, [_move(steps=(first,)), _move(steps=({**first, **variant},)), _give_up()],
        limits=RunLimits(max_moves=6, idle_moves=5),
    )
    dynamic.event_sink = events.append
    monkeypatch.setattr(
        dynamic, "_run_steps",
        lambda decision: MoveOutcome(
            "результатов нет: read.value вернул пусто", "", empty_atoms=("read.value",),
        ),
    )

    dynamic.run(deterministic_error="нет пути")

    assert len(_rejections(events)) == 1


@dataclass
class RepeatedMetricScenario:
    runtime: object

    def __call__(self, text: str) -> ScenarioResult:
        for index in range(2):
            metric = _metric_for_period("6M 2026", 42085.931000000004)
            self.runtime.store.put_artifact(
                OWNER, CASE, node_key=NodeKey(atom_name="read.value", atom_version="1.0.0", args_fingerprint=f"r{index}"),
                output_type="ExtractedMetric", payloads=(metric,), concept=metric.concept, entity_ref=ENTITY,
                period_ref=metric.period_ref, basis=None, document_version="sha256:v1",
                policy_version=self.runtime.policy.policy_version,
            )
        return ScenarioResult(True, "✓ extract")


def test_stopped_summary_lists_each_found_value_once_in_the_users_words(base) -> None:
    dynamic, _ = _run(
        base, [_move(scenarios=("Извлеки выручку",)), "bad", "bad", "bad", "bad"],
        scenario=RepeatedMetricScenario(base), limits=RunLimits(max_moves=6, idle_moves=3),
    )

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped"
    assert "ExtractedMetric" not in result.answer, "имя типа — слово разработчика"
    assert "42085.931000000004" not in result.answer, "двоичный хвост числа не показывается"
    assert "42 085,931" in result.answer
    assert result.answer.count("6M 2026") == 1, "одно и то же значение — одной строкой"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (42085.931000000004, "42 085,931"),
        (100.0, "100"),
        (-1234.5678, "-1 234,568"),
        (1.5e-7, "0,00000015"),
        (-0.0004, "-0,0004"),
        (-0.0, "0"),
    ],
)
def test_summary_number_is_readable_and_never_a_false_zero(value: float, shown: str) -> None:
    assert _shown_value(_metric_for_period("6M 2026", value)) == f"{shown} RUB_MLN"


def test_stopped_summary_speaks_to_the_user_not_about_the_planner(base) -> None:
    dynamic, _ = _run(base, ["bad", "bad", "bad", "bad"], limits=RunLimits(max_moves=12, idle_moves=3))

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped"
    assert "планировщик" not in result.answer.lower()
    assert "цикл" not in result.answer.lower()
    assert "ответа подряд" not in result.answer and "проверку" not in result.answer


def test_the_planner_calls_its_own_model_when_one_is_set(monkeypatch) -> None:
    """Планировщик и автор атомов — разные задачи, владелец ставит им разные модели (08.10.2026).
    Пустое поле — модель бэкенда по умолчанию, как было."""
    seen = []

    async def _complete(prompt, **kwargs):
        seen.append(kwargs.get("model"))
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)
    monkeypatch.setattr(dynamic_run_module.settings, "planner_model", "zai-org/GLM-5.2")
    asyncio.run(dynamic_run_module._default_complete("system", "prompt"))
    monkeypatch.setattr(dynamic_run_module.settings, "planner_model", "")
    asyncio.run(dynamic_run_module._default_complete("system", "prompt"))

    assert seen == ["zai-org/GLM-5.2", None]


def test_the_planner_reply_in_a_markdown_fence_is_still_json(monkeypatch) -> None:
    """`zai-org/GLM-5.2` на Cloud.ru отдаёт верный JSON в ограде ```json (dev, 08.10.2026: 6 из 6
    ответов на боевом промпте) — `response_format` ему не указ. Ограда снимается до разбора."""
    replies = iter(['```json\n{"plan": []}\n```', '```\n{"plan": []}\n```\n', '{"plan": []}', "текст без JSON"])

    async def _complete(prompt, **kwargs):
        return next(replies)

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)

    assert asyncio.run(dynamic_run_module._default_complete("system", "prompt")) == '{"plan": []}'
    assert asyncio.run(dynamic_run_module._default_complete("system", "prompt")) == '{"plan": []}'
    assert asyncio.run(dynamic_run_module._default_complete("system", "prompt")) == '{"plan": []}'
    assert asyncio.run(dynamic_run_module._default_complete("system", "prompt")) == "текст без JSON"
