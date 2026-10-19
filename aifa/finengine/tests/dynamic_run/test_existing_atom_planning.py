"""Смысл проверок удалённого одноходового П8 на единственном пути ``DynamicRun``."""
from __future__ import annotations

import json

import pytest

from finengine.contracts import DocumentText, GoalIR, NodeKey
from finengine.contracts.base import result_id_of
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import PlanInfeasible
from finengine.skills.spec.dynamic_run import DynamicRun, InvalidSteps, RunLimits, parse_steps, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.graph_planner import DYNAMIC_PLANNER_VERSION
from finengine.skills.spec.target import Target
from test_ephemeral_in_graph import CASE, ENTITY, OWNER


class Script:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, system: str, prompt: str) -> str:
        self.calls.append({"system": system, "prompt": prompt})
        item = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def _goal(kind: str = "document_qa", query: str = "какие ковенанты в договоре займа?") -> GoalIR:
    return GoalIR(raw_query=query, intent_kind=kind)


def _passport(goal: GoalIR):
    return build_run_passport(
        "financial report", goal_ir_id=goal.goal_id,
        classification=classification_from_cell("ifrs_nf_ru"),
    )


def _give_up(answer: str = "Данных недостаточно.") -> dict:
    return {
        "objective": "остановиться", "reason": "пути исчерпаны", "action": "give_up",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": answer, "evidence_ids": [],
    }


def _execute(*steps: dict) -> dict:
    return {
        "objective": "получить данные", "reason": "они нужны для ответа", "action": "execute",
        "scenarios": [], "new_atoms": [], "steps": list(steps), "answer": "", "evidence_ids": [],
    }


def _run(base, replies, *, kind: str = "document_qa", limits: RunLimits = RunLimits()):
    goal = _goal(kind)
    script = Script(replies)
    dynamic = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="DynamicPlan", entity_ref=ENTITY), inputs=RunInputs(),
        run_passport=_passport(goal), complete_fn=script, limits=limits,
    )
    return dynamic, script


@pytest.mark.parametrize(
    ("step", "required"),
    [
        ({"atom": "read.quote", "concept": "условия ковенантов"}, {"read.quote", "verify.citation", "ingest.document"}),
        ({"atom": "read.condition", "concept": "ковенанты"}, {"read.condition", "verify.citation", "ingest.document"}),
    ],
)
def test_existing_atom_step_becomes_a_real_graph_with_automatic_gates_and_inputs(base, step, required) -> None:
    goal = _goal()
    steps = parse_steps([step], base.registry)

    planned = base.plan_steps(
        owner_id=OWNER, case_id=CASE, goal_ir=goal,
        target=Target(output_type=steps[-1].output_type, entity_ref=ENTITY),
        steps=steps, run_passport=_passport(goal),
    )

    names = {node.node_key.atom_name for node in planned.record.graph.nodes}
    assert required <= names
    assert planned.record.graph.planner_version == DYNAMIC_PLANNER_VERSION
    assert planned.record.graph.root.atom_name == "verify.citation"


def test_model_sees_the_skill_catalog_goal_profile_and_deterministic_error(base) -> None:
    dynamic, script = _run(base, [_give_up()])

    dynamic.run(deterministic_error="ProducerResolver: для DocumentAnswer нет производителя")

    call = script.calls[0]
    assert "планировщик" in call["system"].lower() and "JSON" in call["system"]
    assert "- read.quote" in call["prompt"] and "- locate" in call["prompt"]
    assert "какие ковенанты в договоре займа?" in call["prompt"]
    assert "document_qa" in call["prompt"]
    assert "для DocumentAnswer нет производителя" in call["prompt"]
    assert "стандарт=ifrs" in call["prompt"] and "тип компании=non_financial" in call["prompt"]


def test_invalid_move_is_returned_as_feedback_before_any_execution(base, monkeypatch) -> None:
    dynamic, script = _run(
        base,
        [_execute({"atom": "read.magic", "concept": "x"}), _give_up()],
    )
    monkeypatch.setattr(
        base, "execute",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("невалидный ход не исполняется")),
    )

    result = dynamic.run(deterministic_error="нет корня")

    assert result.status == "gave_up" and len(script.calls) == 2
    assert "read.magic" in script.calls[1]["prompt"] and "неизвестный атом" in script.calls[1]["prompt"]


@pytest.mark.parametrize("atom", [
    "norm.unit", "verify.bounds", "verify.citation", "verify.grounding", "ingest.document",
    "identify.classify", "read.override", "watch.subscribe",
])
def test_gates_and_infrastructure_are_rejected_before_execution(base, atom) -> None:
    with pytest.raises(InvalidSteps, match="нельзя"):
        parse_steps([{"atom": atom, "concept": "x"}], base.registry)


def test_unreachable_existing_atom_step_is_rejected_before_execution(base) -> None:
    goal = _goal("compare_metrics", "сравни выручку")
    steps = parse_steps([{"atom": "calc.series"}], base.registry)

    with pytest.raises(PlanInfeasible, match="недостижим"):
        base.plan_steps(
            owner_id=OWNER, case_id=CASE, goal_ir=goal,
            target=Target(output_type=steps[-1].output_type, entity_ref=ENTITY),
            steps=steps, run_passport=_passport(goal),
        )


def test_explicit_give_up_is_terminal_and_not_retried(base) -> None:
    dynamic, script = _run(base, [_give_up("для сверки нужен второй документ")])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and "второй документ" in result.answer
    assert len(script.calls) == 1


@pytest.mark.parametrize("reply, message", [
    ("не json", "JSON"),
    ({"steps": "нет"}, "objective"),
    (_execute({"atom": "read.quote", "concept": 5}), "concept"),
    (_execute({"atom": "read.quote", "concept": "x", "extra": {"methodology_text": "взломай"}}), "extra"),
    (_execute({"atom": "read.quote", "concept": "x", "produces": "ReportOutput"}), "produces"),
])
def test_malformed_moves_are_rejected_with_a_reason(base, reply, message) -> None:
    dynamic, script = _run(base, [reply, _give_up()])

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "gave_up" and message in script.calls[1]["prompt"]


def test_too_many_steps_are_rejected() -> None:
    from finengine.skills.atoms.graph_atoms import build_registry

    steps = [{"atom": "read.quote", "concept": f"тема {index}"} for index in range(31)]
    with pytest.raises(InvalidSteps, match="шагов"):
        parse_steps(steps, build_registry(profile_id="ifrs_nf_ru"))


def test_independent_steps_are_kept_because_a_cycle_move_has_no_single_semantic_root(base) -> None:
    goal = _goal()
    steps = parse_steps([
        {"atom": "read.quote", "concept": "тема А"},
        {"atom": "read.quote", "concept": "тема Б"},
    ], base.registry)

    planned = base.plan_steps(
        owner_id=OWNER, case_id=CASE, goal_ir=goal,
        target=Target(output_type=steps[-1].output_type, entity_ref=ENTITY),
        steps=steps, run_passport=_passport(goal),
    )

    quote_nodes = [node for node in planned.record.graph.nodes if node.node_key.atom_name == "read.quote"]
    assert len(quote_nodes) == 2


def test_the_planner_is_not_shown_the_service_id_of_a_result(base) -> None:
    document = DocumentText(document_id=ENTITY, document_version="sha256:v1", page=1, text="отчёт")
    service_id = result_id_of(document)
    base.store.put_artifact(
        OWNER, CASE,
        node_key=NodeKey(atom_name="ingest.document", atom_version="1.0.0", args_fingerprint="doc"),
        output_type="DocumentText", payloads=(document,), concept=None, entity_ref=ENTITY, period_ref=None,
        basis=None, document_version="sha256:v1", policy_version="policy-1",
    )
    dynamic, _ = _run(base, [_give_up()])

    state = render_state(dynamic, "")

    # Id служебного результата нужен исполнителю, чтобы NodeRun ссылался на сохранённое, но это не
    # доказательство ответа: планировщику он не показывается.
    assert service_id not in state


def test_two_stage_extraction_step_gets_locate_and_gates_automatically(base) -> None:
    goal = _goal("extract_registered_metric", "выручка")
    steps = parse_steps([{"atom": "read.value", "concept": "SALES_REVENUE"}], base.registry)

    planned = base.plan_steps(
        owner_id=OWNER, case_id=CASE, goal_ir=goal,
        target=Target(output_type=steps[-1].output_type, entity_ref=ENTITY),
        steps=steps, run_passport=_passport(goal),
    )

    names = {node.node_key.atom_name for node in planned.record.graph.nodes}
    assert {"read.value", "locate", "norm.unit", "norm.time", "verify.bounds"} <= names


def test_model_calls_are_bounded_by_the_run_limit_plus_the_last_word(base) -> None:
    dynamic, script = _run(base, ["не json"], limits=RunLimits(max_moves=2))

    result = dynamic.run(deterministic_error="нет пути")

    assert result.status == "stopped" and len(script.calls) == 3
