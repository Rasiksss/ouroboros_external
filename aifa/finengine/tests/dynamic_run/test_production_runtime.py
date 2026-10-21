"""Подключение ``DynamicRun`` к production ingress (часть 5)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from finengine.contracts import ExtractedMetric, GoalIR, ScalarFact
from finengine.skills.atoms.intent import IntentDecision, IntentParseError, RootTarget
from finengine.skills.case_runtime import PlanInfeasible
from finengine.skills.production_runtime import (
    EvidenceGateOutcome,
    ProductionContext,
    ProductionOutcome,
    ScenarioChannel,
    _dynamic_outcome,
    execute_production_goal,
    execute_routed_goal,
)
from finengine.skills.spec.dynamic_run import DynamicRunResult, JournalEntry, ScenarioResult
from finengine.skills.spec.target import Target


def _goal(kind: str = "document_qa") -> GoalIR:
    return GoalIR(raw_query="сложный вопрос", intent_kind=kind)


def _context(runtime) -> ProductionContext:
    return ProductionContext(
        runtime=runtime, passport=SimpleNamespace(entity=None), owner_id="owner-1",
        case_id="case-1", document_id="doc-1", document_version="sha256:v1",
    )


def _result(status: str = "finalized", *, evidence=(), answer: str = "Ответ") -> DynamicRunResult:
    return DynamicRunResult(status=status, answer=answer, evidence=tuple(evidence), journal=())


def _metric() -> ExtractedMetric:
    return ExtractedMetric.model_construct(
        concept="EBITDA", period_ref="2024",
        scalar_fact=ScalarFact.model_construct(value=100.0, unit="RUB"),
    )


class FailingRuntime:
    def __init__(self) -> None:
        self.store = SimpleNamespace(put_ad_hoc_spec=lambda *args, **kwargs: None)
        self.executed = False

    def plan(self, **kwargs):
        raise PlanInfeasible("нет производителя")

    def execute(self, **kwargs):
        self.executed = True
        raise AssertionError("после перехода в DynamicRun старый план исполняться не должен")


class CapturingDynamicRun:
    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls.append(kwargs)

    def run(self, *, deterministic_error: str) -> DynamicRunResult:
        self.kwargs["deterministic_error"] = deterministic_error
        return _result()


def test_plan_infeasible_starts_dynamic_run_instead_of_the_old_one_shot_planner(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    CapturingDynamicRun.calls.clear()
    monkeypatch.setattr(production, "DynamicRun", CapturingDynamicRun)
    runtime = FailingRuntime()
    context = _context(runtime)
    goal = _goal()
    root = RootTarget(Target(output_type="DocumentAnswer", entity_ref="ent-1"), {})

    outcome = execute_production_goal(
        context, goal, document_text="отчёт", roots=[root], allow_dynamic=True,
    )

    assert outcome.dynamic == _result()
    assert not outcome.roots and not runtime.executed
    call = CapturingDynamicRun.calls[0]
    assert call["runtime"] is runtime and call["goal"] is goal
    assert call["inputs"].document_text == "отчёт"
    assert call["deterministic_error"] == "нет производителя"
    assert isinstance(call["run_scenario"], ScenarioChannel)


def test_finalized_dynamic_result_becomes_manifest_and_publishes_typed_evidence() -> None:
    events: list[dict] = []
    metric = _metric()

    outcome = _dynamic_outcome(
        _result(evidence=(metric,)), events.append, case_id="case-1", document_id="doc-1",
    )

    assert outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert outcome.manifest["dynamic_run"]["answer"] == "Ответ"
    assert events == [{
        "type": "typed_result", "status": "complete", "result": metric.model_dump(mode="json"),
    }]


@pytest.mark.parametrize("status", ["gave_up", "stopped"])
def test_non_final_dynamic_result_is_an_answer_with_partial_results(status: str) -> None:
    events: list[dict] = []
    metric = _metric()

    outcome = _dynamic_outcome(
        _result(status, evidence=(metric,), answer="Недостаточно данных"), events.append,
        case_id="case-1", document_id="doc-1",
    )

    assert outcome.manifest["dynamic_run"]["status"] == status
    assert "error" not in outcome.manifest
    assert [event["type"] for event in events] == ["typed_result"]
    assert events[0]["result"] == metric.model_dump(mode="json")


def test_dynamic_events_use_the_checked_sink_and_typed_result_waits_for_finalization(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    metric = _metric()
    events: list[dict] = []
    context = _context(SimpleNamespace())

    monkeypatch.setattr(production, "prepare_production_context", lambda **kwargs: context)

    def _execute(context, goal, **kwargs):
        sink = kwargs["event_sink"]
        sink({"type": "plan_graph", "planner_version": "dynamic-test"})
        sink({"type": "node_run", "status": "succeeded"})
        sink({"type": "typed_result", "result": {"premature": True}})
        return ProductionOutcome(context=context, dynamic=_result(evidence=(metric,)))

    monkeypatch.setattr(production, "execute_production_goal", _execute)

    outcome = execute_routed_goal(
        owner_id="owner-1", case_id="case-1", document_id="doc-1", document_text="отчёт",
        goal=_goal("extract_structure"), event_sink=events.append,
    )

    assert outcome is not None and outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert [event["type"] for event in events] == ["plan_graph", "node_run", "typed_result"]
    assert events[0]["planner_version"].startswith("dynamic")
    assert events[-1]["result"] == metric.model_dump(mode="json")


def test_scenario_channel_uses_chat_parser_and_hides_terminal_events(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    goal = GoalIR(raw_query="извлеки EBITDA", intent_kind="extract_registered_metric")
    context = _context(SimpleNamespace(store=None))
    events: list[dict] = []
    seen: dict = {}

    async def _parse(request, *, context, allow_answer):
        seen.update(request=request, ingress=context, allow_answer=allow_answer)
        return IntentDecision("plan", goal)

    def _execute(**kwargs):
        seen["execute"] = kwargs
        kwargs["event_sink"]({"type": "plan_graph"})
        kwargs["event_sink"]({"type": "node_run"})
        kwargs["event_sink"]({"type": "typed_result"})
        kwargs["event_sink"]({"type": "typed_error"})
        return EvidenceGateOutcome(
            "financial_pipeline",
            {"goal_type": goal.intent_kind, "result": {"output_type": "ExtractedMetric"}},
        )

    monkeypatch.setattr(production, "parse_message", _parse)
    monkeypatch.setattr(production, "execute_routed_goal", _execute)
    channel = ScenarioChannel(context=context, document_text="отчёт", role="risks", event_sink=events.append)

    result = channel("извлеки EBITDA")

    assert result == ScenarioResult(True, "✓ extract_registered_metric: ExtractedMetric")
    assert seen["allow_answer"] is False and seen["ingress"].ui_role == "risks"
    assert seen["execute"]["allow_dynamic"] is False
    assert [event["type"] for event in events] == ["plan_graph", "node_run"]


def test_scenario_channel_turns_intent_parse_error_into_move_feedback(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    async def _parse(*args, **kwargs):
        raise IntentParseError("невалидный ответ")

    monkeypatch.setattr(production, "parse_message", _parse)
    channel = ScenarioChannel(context=_context(SimpleNamespace()), document_text="", role="", event_sink=None)

    assert channel("запрос") == ScenarioResult(False, "✗ запрос не разобран: невалидный ответ")


def test_allow_dynamic_false_refuses_a_goal_without_a_deterministic_root(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    monkeypatch.setattr(
        production, "DynamicRun",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("вложенный DynamicRun запрещён")),
    )

    outcome = execute_routed_goal(
        owner_id="owner-1", case_id="case-1", document_id="doc-1", document_text="отчёт",
        goal=_goal("extract_structure"), allow_dynamic=False,
    )

    assert outcome is not None
    assert outcome.manifest["error"]["code"] == "scenario_not_deterministic"


def test_allow_dynamic_false_turns_plan_infeasible_into_scenario_refusal(monkeypatch) -> None:
    from finengine.skills import production_runtime as production

    context = _context(SimpleNamespace())
    monkeypatch.setattr(production, "prepare_production_context", lambda **kwargs: context)
    monkeypatch.setattr(
        production, "execute_production_goal",
        lambda *args, **kwargs: (_ for _ in ()).throw(PlanInfeasible("нет пути")),
    )
    monkeypatch.setattr(
        production, "DynamicRun",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("вложенный DynamicRun запрещён")),
    )

    outcome = execute_routed_goal(
        owner_id="owner-1", case_id="case-1", document_id="doc-1", document_text="отчёт",
        goal=GoalIR(
            raw_query="извлеки выручку", intent_kind="extract_registered_metric",
            targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
        ), allow_dynamic=False,
    )

    assert outcome is not None
    assert outcome.manifest["error"]["code"] == "scenario_not_deterministic"
    assert "нет пути" in outcome.manifest["error"]["message"]


def test_local_chat_path_builds_a_dynamic_manifest_without_a_station(base, local_room, monkeypatch) -> None:
    """Настоящий ingress → DynamicRun → граф → временный атом → манифест.

    Модели записаны ответами, а код атома исполняется фикстурой ``local_room``;
    продуктового локального fallback здесь нет.
    """
    from finengine.skills import production_runtime as production
    from finengine.skills.spec import dynamic_run
    from subtask_support import SubtaskCodes, patch_subtask

    planner_calls: list[str] = []
    moves = [
        {
            "objective": "извлечь EBITDA", "reason": "это вход расчёта", "action": "execute",
            "scenarios": [], "new_atoms": [],
            "steps": [{"atom": "read.value", "concept": "EBITDA"}],
            "answer": "", "evidence_ids": [],
        },
        {
            "objective": "удвоить EBITDA", "reason": "это просил пользователь", "action": "execute",
            "scenarios": [],
            "new_atoms": [{
                "name": "eph.double_ebitda", "purpose": "удвоить EBITDA",
                "requires": [{"input_type": "ExtractedMetric", "concept": "EBITDA"}],
                "output_type": "dyn:double_ebitda",
                "output_schema": {
                    "type": "object", "required": ["value"],
                    "properties": {"value": {"type": "number", "x-unit": "RUB"}},
                },
            }],
            "steps": [{"atom": "eph.double_ebitda"}], "answer": "", "evidence_ids": [],
        },
    ]

    async def _planner(system: str, prompt: str) -> str:
        planner_calls.append(prompt)
        if len(planner_calls) <= len(moves):
            return json.dumps(moves[len(planner_calls) - 1], ensure_ascii=False)
        line = next(line for line in prompt.splitlines() if "| dyn:double_ebitda |" in line)
        evidence_id = line.split("|", 1)[0].strip()
        return json.dumps({
            "objective": "ответить", "reason": "расчёт готов", "action": "finalize",
            "scenarios": [], "new_atoms": [], "steps": [], "answer": "Расчёт готов.",
            "evidence_ids": [evidence_id],
        }, ensure_ascii=False)

    monkeypatch.setattr(production, "build_registry", lambda **kwargs: base.registry)
    monkeypatch.setattr(dynamic_run, "_default_complete", _planner)
    patch_subtask(monkeypatch, SubtaskCodes([
        "def run(inputs):\n"
        "    metric = inputs['ExtractedMetric'][0]\n"
        "    return {'value': metric['scalar_fact']['value'] * 2}\n"
    ]))
    events: list[dict] = []

    outcome = execute_routed_goal(
        owner_id="owner-1", case_id="chat-case", document_id="chat-doc",
        document_text="financial report", goal=_goal("document_qa"),
        store=base.store, event_sink=events.append,
    )

    assert outcome is not None
    manifest = outcome.manifest["dynamic_run"]
    assert manifest["status"] == "finalized" and manifest["answer"] == "Расчёт готов.", {
        "manifest": manifest, "planner_calls": planner_calls,
    }
    assert manifest["evidence"][0]["output_type"] == "dyn:double_ebitda"
    assert manifest["evidence"][0]["payload"]["value"] == 1000.0
    graphs = [event for event in events if event.get("type") == "plan_graph"]
    assert graphs and all(event["planner_version"].startswith("dynamic") for event in graphs)


def _dynamic_run_raising(error: Exception):
    class RaisingDynamicRun:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, deterministic_error: str):
            raise error

    return RaisingDynamicRun


def _enter_dynamic_run(runtime):
    return execute_production_goal(
        _context(runtime), _goal(), document_text="отчёт",
        roots=[RootTarget(Target(output_type="DocumentAnswer", entity_ref="ent-1"), {})],
        allow_dynamic=True,
    )


def test_a_crash_inside_the_cycle_is_a_typed_refusal_with_the_reason(monkeypatch) -> None:
    """Иначе чат скажет «конвейер временно недоступен» и причина останется только в логе."""
    from finengine.skills import production_runtime as production

    monkeypatch.setattr(production, "DynamicRun", _dynamic_run_raising(RuntimeError("станция не отвечает")))

    outcome = _enter_dynamic_run(FailingRuntime())

    assert outcome.dynamic is None
    error = outcome.refusal_event["error"]
    assert error["code"] == "dynamic_run_failed" and "станция не отвечает" in error["message"]


def test_the_llm_budget_stop_is_not_swallowed_by_the_cycle_refusal(monkeypatch) -> None:
    from finengine.llm.budget import BudgetExceeded
    from finengine.skills import production_runtime as production

    monkeypatch.setattr(production, "DynamicRun", _dynamic_run_raising(BudgetExceeded("лимит")))

    with pytest.raises(BudgetExceeded):
        _enter_dynamic_run(FailingRuntime())


def test_scenario_channel_runs_in_the_store_of_the_cycle(monkeypatch) -> None:
    """Сценарий обязан писать артефакты в тот же `Case`, который читает планировщик."""
    from finengine.skills import production_runtime as production

    goal = GoalIR(raw_query="извлеки EBITDA", intent_kind="extract_registered_metric")
    store = object()
    seen: dict = {}

    async def _parse(request, *, context, allow_answer):
        return IntentDecision("plan", goal)

    def _execute(**kwargs):
        seen.update(kwargs)
        return EvidenceGateOutcome("financial_pipeline", {"goal_type": goal.intent_kind})

    monkeypatch.setattr(production, "parse_message", _parse)
    monkeypatch.setattr(production, "execute_routed_goal", _execute)
    channel = ScenarioChannel(
        context=_context(SimpleNamespace(store=store)), document_text="отчёт", role="", event_sink=None,
    )

    channel("извлеки EBITDA")

    assert seen["store"] is store


def test_dynamic_manifest_does_not_carry_a_whole_downloaded_file() -> None:
    """Скачанный атомом файл лежит в `dyn:*` целиком; в манифест (и историю диалога) уходит его начало."""
    from finengine.contracts import DynamicArtifact

    download = DynamicArtifact.model_construct(
        evidence_id="ev-1", output_type="dyn:download",
        payload={"url": "https://example.org/a.csv", "content_type": "text/csv", "text": "x" * 500_000},
        origin=[], producer="eph._download@v1", input_evidence_ids=[],
    )

    outcome = _dynamic_outcome(
        _result(evidence=(download,)), None, case_id="case-1", document_id="doc-1",
    )

    item = outcome.manifest["dynamic_run"]["evidence"][0]
    assert item["evidence_id"] == "ev-1" and item["output_type"] == "dyn:download"
    assert item["payload"]["url"] == "https://example.org/a.csv"
    assert len(json.dumps(outcome.manifest)) < 20_000
    assert "урезано" in item["payload"]["text"]


def test_dynamic_manifest_does_not_carry_whole_move_outcomes() -> None:
    """Итог хода в журнале — строка на каждый узел с хвостом ошибки; в историю диалога уходит его начало."""
    journal = tuple(
        JournalEntry("получить данные", "нужны для ответа", "ход завершён с ошибками\n" + "✗ eph.calc\n" * 5_000)
        for _ in range(12)
    )
    result = DynamicRunResult(status="gave_up", answer="Данных нет.", evidence=(), journal=journal)

    outcome = _dynamic_outcome(result, None, case_id="case-1", document_id="doc-1")

    moves = outcome.manifest["dynamic_run"]["moves"]
    assert len(moves) == 12 and moves[0]["outcome"].startswith("ход завершён с ошибками")
    assert "урезано" in moves[0]["outcome"]
    assert len(json.dumps(outcome.manifest, ensure_ascii=False)) < 40_000
    assert outcome.manifest["dynamic_run"]["answer"] == "Данных нет."
