"""Часть 13 доводки: любой сбой детерминированного планирования — один ``DynamicRun`` на всё сообщение.

Вход — настоящий ``execute_routed_goal`` (и настоящий ``execute_production_goal`` под ним) на
настоящем ``CaseRuntime`` с SQLite. Подставной только ``DynamicRun`` (и модель классификатора там,
где разбор идёт через ``parse_message``).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest

from finengine.contracts import GoalIR
from finengine.skills.atoms.intent import IngressContext, parse_message
from finengine.skills.case_runtime import CaseRuntime, PlanInfeasible
from finengine.skills import production_runtime as production
from finengine.skills.production_runtime import _COMPOUND_METRIC_BUDGET, execute_routed_goal
from finengine.skills.spec.dynamic_run import DynamicRun, DynamicRunResult, RunLimits, render_state
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target
from tests.integration.ingress_support import set_intent_sequence

from test_ephemeral_in_graph import CASE, ENTITY, OWNER

ROOT = Path(__file__).resolve().parents[2]
MESSAGE = "извлеки выручку и посчитай для неё коэффициент вариации"
ANALYSIS_PART = "посчитай для неё коэффициент вариации"


def _clause(ref: str, kind: str, *, concept: str | None = None, depends_on=(), unresolved=(), **extra) -> dict:
    return {
        "goal_ref": ref, "raw_query": MESSAGE, "intent_kind": kind,
        "targets": [{"concept": concept, "output_kind": "scalar"}] if concept else [],
        "depends_on": list(depends_on), "unresolved": list(unresolved), **extra,
    }


def _compound(*parts: dict) -> GoalIR:
    first, *rest = parts
    return GoalIR(raw_query=MESSAGE, additional_goals=rest, **{k: v for k, v in first.items() if k != "raw_query"})


def extract_plus_analysis() -> GoalIR:
    """Исходный случай части 13: разбор приёмки 06.10.2026."""
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "custom_analysis", depends_on=["goal-1"], unresolved=[ANALYSIS_PART]),
    )


def single_analysis() -> GoalIR:
    return GoalIR(raw_query=MESSAGE, intent_kind="custom_analysis", unresolved=[MESSAGE])


def broken_root_in_compound() -> GoalIR:
    """Путь 2: у второй части не строится корень (формулы нет, ``root_from_goal`` бросает ValueError)."""
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "calculate_ad_hoc_metric", unresolved=["оборачиваемость запасов"]),
    )


def broken_root_single() -> GoalIR:
    return GoalIR(
        raw_query="посчитай оборачиваемость запасов", intent_kind="calculate_ad_hoc_metric",
        unresolved=["оборачиваемость запасов"],
    )


def cyclic_compound() -> GoalIR:
    """Путь 3: корни построены, а общий граф собрать нельзя (зависимости образуют цикл)."""
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE", depends_on=["goal-2"]),
        _clause("goal-2", "extract_registered_metric", concept="NET_INCOME", depends_on=["goal-1"]),
    )


class SpyRun:
    """Подставной ``DynamicRun``: запоминает, с чем его создали, и сразу отвечает."""

    calls: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls.append(kwargs)

    def run(self, *, deterministic_error: str) -> DynamicRunResult:
        self.kwargs["deterministic_error"] = deterministic_error
        return DynamicRunResult(status="finalized", answer="Ответ цикла", evidence=(), journal=())


@pytest.fixture
def spy(monkeypatch) -> type[SpyRun]:
    SpyRun.calls = []
    monkeypatch.setattr(production, "DynamicRun", SpyRun)
    return SpyRun


@pytest.fixture
def budgets(monkeypatch, base) -> list:
    """Бюджет, с которым ``execute_routed_goal`` собрал рантайм каждого входа."""
    seen: list = []
    real = production.prepare_production_context

    def _prepare(**kwargs):
        seen.append(kwargs.get("budget"))
        return real(**kwargs)

    monkeypatch.setattr(production, "build_registry", lambda **kwargs: base.registry)
    monkeypatch.setattr(production, "prepare_production_context", _prepare)
    return seen


def _route(base, goal: GoalIR, *, allow_dynamic: bool = True, events: list | None = None):
    return execute_routed_goal(
        owner_id=OWNER, case_id="chat-case", document_id="chat-doc", document_text="financial report",
        goal=goal, store=base.store, event_sink=events.append if events is not None else None,
        allow_dynamic=allow_dynamic,
    )


# ---- три пути составной цели и сбой корня одиночной цели ------------------------------------

def two_extracts() -> GoalIR:
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "extract_registered_metric", concept="NET_INCOME"),
    )


def _stub_plan_many(monkeypatch) -> None:
    """``execute_routed_goal`` собирает свой ``CaseRuntime``, поэтому подмена — на классе."""
    monkeypatch.setattr(
        CaseRuntime, "plan_many",
        lambda self, **kwargs: (_ for _ in ()).throw(PlanInfeasible("общий граф не собрался")),
    )


@pytest.mark.parametrize(
    ("make_goal", "reason"),
    [
        pytest.param(extract_plus_analysis, "custom_analysis", id="path1-compound-with-dynamic-part"),
        pytest.param(broken_root_in_compound, "формул", id="path2-compound-root-not-built"),
        pytest.param(broken_root_single, "формул", id="single-root-not-built"),
        pytest.param(cyclic_compound, "cycle", id="path3-plan-many-refuses-real-planner"),
    ],
)
def test_planning_failure_starts_one_dynamic_run_for_the_whole_message(
    make_goal, reason, base, spy, budgets,
) -> None:
    goal = make_goal()

    outcome = _route(base, goal)

    assert outcome is not None and "error" not in outcome.manifest, outcome.manifest
    assert outcome.manifest["dynamic_run"]["answer"] == "Ответ цикла"
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["goal"] is goal, "цикл получает цель целиком, со всеми частями"
    assert call["target"].output_type == "DynamicPlan", "маркерный корень, а не корень одной из частей"
    assert call["inputs"].document_text == "financial report"
    assert reason in call["deterministic_error"], "причина сбоя доходит до планировщика"


def test_path3_plan_infeasible_from_plan_many_starts_the_cycle(base, spy, budgets, monkeypatch) -> None:
    _stub_plan_many(monkeypatch)
    goal = two_extracts()

    outcome = _route(base, goal)

    assert outcome is not None and outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert len(spy.calls) == 1 and spy.calls[0]["goal"] is goal
    assert "общий граф не собрался" in spy.calls[0]["deterministic_error"]


# ---- ограждения -------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "make_goal",
    [extract_plus_analysis, broken_root_in_compound, broken_root_single, cyclic_compound, single_analysis],
)
def test_nested_scenario_never_starts_a_cycle(make_goal, base, spy, budgets) -> None:
    outcome = _route(base, make_goal(), allow_dynamic=False)

    assert outcome is not None
    assert outcome.manifest["error"]["code"] == "scenario_not_deterministic"
    assert not spy.calls, "цикла внутри цикла нет"


def test_nested_scenario_refuses_when_plan_many_is_infeasible(base, spy, budgets, monkeypatch) -> None:
    _stub_plan_many(monkeypatch)

    outcome = _route(base, two_extracts(), allow_dynamic=False)

    assert outcome.manifest["error"]["code"] == "scenario_not_deterministic"
    assert "общий граф не собрался" in outcome.manifest["error"]["message"]
    assert not spy.calls


_AMBIGUITY = [{"term": "коэффициент", "candidates": ["CV", "BETA"], "reason": "какой именно?"}]


def test_ambiguity_stays_a_question_to_the_user(base, spy, budgets) -> None:
    goal = GoalIR(raw_query="посчитай коэффициент", intent_kind="custom_analysis", ambiguities=_AMBIGUITY)

    outcome = _route(base, goal)

    assert outcome.manifest["error"]["code"] == "goal_needs_clarification"
    assert not spy.calls


def test_ambiguity_of_a_compound_goal_stays_a_question_to_the_user(base, spy, budgets) -> None:
    goal = extract_plus_analysis().model_copy(update={"ambiguities": GoalIR(
        raw_query="x", intent_kind="custom_analysis", ambiguities=_AMBIGUITY,
    ).ambiguities})

    outcome = _route(base, goal)

    assert outcome.manifest["error"]["code"] == "goal_needs_clarification"
    assert not spy.calls


def _ambiguous(ref: str, kind: str, **extra) -> dict:
    return _clause(ref, kind, ambiguities=_AMBIGUITY, **extra)


@pytest.mark.parametrize(
    "make_goal",
    [
        pytest.param(
            lambda: _compound(
                _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
                _ambiguous("goal-2", "custom_analysis", depends_on=["goal-1"], unresolved=[ANALYSIS_PART]),
            ),
            id="dynamic-part",
        ),
        pytest.param(
            lambda: _compound(
                _clause("goal-1", "custom_analysis", unresolved=[ANALYSIS_PART]),
                _ambiguous("goal-2", "extract_registered_metric"),
            ),
            id="deterministic-part-after-dynamic-primary",
        ),
        pytest.param(
            lambda: _compound(
                _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
                _ambiguous("goal-2", "extract_registered_metric"),
            ),
            id="second-of-two-deterministic-parts",
        ),
    ],
)
@pytest.mark.parametrize("allow_dynamic", [True, False])
def test_ambiguity_in_any_part_of_a_compound_goal_stays_a_question_to_the_user(
    make_goal, allow_dynamic, base, spy, budgets,
) -> None:
    """Р13.2: неоднозначность — единственное исключение из входа в цикл, в какой бы части она ни стояла."""
    outcome = _route(base, make_goal(), allow_dynamic=allow_dynamic)

    error = outcome.manifest["error"]
    assert error["code"] == "goal_needs_clarification"
    assert "коэффициент" in error["message"], "вопрос называет неоднозначный термин"
    assert not spy.calls


# ---- причина в логе ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "make_goal", [extract_plus_analysis, broken_root_single, broken_root_in_compound, cyclic_compound],
)
def test_the_reason_of_a_planning_failure_is_logged_and_is_not_an_error(make_goal, base, spy, budgets, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="finengine.skills.production_runtime"):
        _route(base, make_goal())

    records = [r for r in caplog.records if "dynamic run" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert spy.calls[0]["deterministic_error"] in records[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---- ошибка кода планировщика: одиночная и составная цель ведут себя одинаково ------------------------

_REGISTRY_BROKEN = "атом out.narrative объявляет обязательный этап 'verify.grounding' — контракт реестра нарушен"


def _planner_raises_value_error(monkeypatch) -> None:
    from finengine.skills.spec.graph_planner import GraphPlanner

    def _broken(self, *args, **kwargs):
        raise ValueError(_REGISTRY_BROKEN)

    monkeypatch.setattr(GraphPlanner, "plan_ex", _broken)


@pytest.mark.parametrize("make_goal", [two_extracts, lambda: GoalIR(
    raw_query="выручка", intent_kind="extract_registered_metric",
    targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
)], ids=["compound", "single"])
def test_a_planner_value_error_starts_the_cycle_for_single_and_compound_goals_alike(
    make_goal, base, spy, budgets, monkeypatch,
) -> None:
    """Замысел планировщика: ошибка кода детерминированного сценария подхватывается циклом."""
    _planner_raises_value_error(monkeypatch)

    outcome = _route(base, make_goal())

    assert outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert len(spy.calls) == 1 and _REGISTRY_BROKEN in spy.calls[0]["deterministic_error"]


def test_a_planner_value_error_in_a_nested_scenario_is_refused_to_the_planner(
    base, spy, budgets, monkeypatch,
) -> None:
    _planner_raises_value_error(monkeypatch)

    outcome = _route(base, GoalIR(
        raw_query="выручка", intent_kind="extract_registered_metric",
        targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
    ), allow_dynamic=False)

    assert outcome.manifest["error"]["code"] == "scenario_not_deterministic"
    assert not spy.calls


def test_a_masked_code_error_leaves_a_traceback_and_an_expected_failure_does_not(
    base, spy, budgets, monkeypatch, caplog,
) -> None:
    """Вход в цикл не должен прятать поломку кода: у неё в логе трассировка, у штатного сбоя — нет."""
    with caplog.at_level(logging.WARNING, logger="finengine.skills.production_runtime"):
        _route(base, broken_root_single())
        _planner_raises_value_error(monkeypatch)
        _route(base, two_extracts())

    expected, masked = [r for r in caplog.records if "dynamic run" in r.getMessage()]
    assert expected.exc_info is None
    assert masked.exc_info is not None and _REGISTRY_BROKEN in str(masked.exc_info[1].__cause__)


# ---- бюджет цикла ------------------------------------------------------------------------------

@pytest.mark.parametrize("make_goal", [extract_plus_analysis, broken_root_in_compound, broken_root_single])
def test_a_goal_entering_the_cycle_gets_the_budget_of_a_single_custom_analysis(
    make_goal, base, spy, budgets,
) -> None:
    _route(base, single_analysis())
    _route(base, make_goal())

    assert budgets[0] is None
    assert budgets[1] is None, "бюджет сборки «составных показателей» к циклу не относится"


def test_the_primary_analysis_kind_does_not_widen_the_budget_of_the_cycle(base, spy, budgets) -> None:
    goal = _compound(
        _clause("goal-1", "full_analysis"),
        _clause("goal-2", "custom_analysis", depends_on=["goal-1"], unresolved=[ANALYSIS_PART]),
    )

    _route(base, goal)

    assert budgets == [None]
    assert "methodology_text" not in spy.calls[0]["root_extra"]


def test_path3_keeps_the_compound_budget_because_the_runtime_exists_before_plan_many(
    base, spy, budgets,
) -> None:
    _route(base, cyclic_compound())

    assert budgets == [_COMPOUND_METRIC_BUDGET]


# ---- render_state: состояние показывает все части ----------------------------------------------

def _state(base, goal: GoalIR) -> str:
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="DynamicPlan", entity_ref=ENTITY), inputs=RunInputs(),
        complete_fn=lambda system, prompt: None, limits=RunLimits(),
    )
    return render_state(run, "")


def _goal_block(state: str) -> str:
    return state.split("## Разобранная цель\n", 1)[1].split("\n\n## Профиль отчёта", 1)[0]


def test_state_lists_every_part_of_a_compound_goal_with_its_dependency(base) -> None:
    block = _goal_block(_state(base, extract_plus_analysis()))

    assert "goal-1" in block and "extract_registered_metric" in block and "SALES_REVENUE" in block
    assert "goal-2" in block and "custom_analysis" in block
    assert "depends_on=['goal-1']" in block
    assert ANALYSIS_PART in block, "текст динамической части из unresolved"


def test_state_of_a_single_goal_keeps_its_meaning(base) -> None:
    block = _goal_block(_state(base, single_analysis()))

    assert block.splitlines()[0].startswith("goal_type=custom_analysis; показатели=[]")
    assert "depends_on" not in block and "goal-" not in block


def test_state_with_a_compound_goal_stays_small(base) -> None:
    assert len(_goal_block(_state(base, extract_plus_analysis()))) < 1_000


# ---- контракты между слоями ----------------------------------------------------------------------

_PLAN = {"route": "plan", "targets": [], "entity_ref": None, "period_ref": None, "role": None, "ambiguities": []}
COMPOUND_FORM = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
    "additional_goals": [{
        **_PLAN, "goal_ref": "goal-2", "intent_kind": "custom_analysis", "depends_on": ["goal-1"],
        "unresolved": [ANALYSIS_PART],
    }],
}
SINGLE_FORM = {**_PLAN, "intent_kind": "custom_analysis", "unresolved": [MESSAGE]}


def _classify(monkeypatch, payload: dict) -> GoalIR:
    set_intent_sequence(monkeypatch, [payload])
    decision = asyncio.run(parse_message(MESSAGE, context=IngressContext(ui_role=""), allow_answer=False))
    return decision.goal


def test_K13_1_both_forms_of_the_classifier_reach_the_same_cycle_entry(monkeypatch, base, spy, budgets) -> None:
    entries = []
    for payload in (COMPOUND_FORM, SINGLE_FORM):
        spy.calls.clear()
        _route(base, _classify(monkeypatch, payload))
        assert len(spy.calls) == 1
        call = spy.calls[0]
        entries.append((
            call["goal"].raw_query, call["target"].output_type, call["inputs"].document_text,
            bool(call["deterministic_error"]),
        ))

    assert entries[0] == entries[1] == (MESSAGE, "DynamicPlan", "financial report", True)


def test_K13_3_what_the_classifier_returned_is_readable_in_the_planner_state(monkeypatch, base) -> None:
    goal = _classify(monkeypatch, COMPOUND_FORM)
    block = _goal_block(_state(base, goal))
    prompt = (ROOT / "src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")

    assert "goal-1" in block and "goal-2" in block and "SALES_REVENUE" in block
    assert "depends_on=['goal-1']" in block and ANALYSIS_PART in block
    # имя поля, которым пользуется промпт, совпадает с тем, что печатает состояние
    assert "`depends_on`" in prompt and "depends_on=" in block


def test_K13_6_the_planner_prompt_in_code_matches_its_copy_in_the_plan() -> None:
    plan = (ROOT / "docs/build-plan/34-gibridnyy-planirovshchik-dynamic-run.md").read_text(encoding="utf-8")
    section = plan.split("### 6.1. Системный промпт — `dynamic_run_planner.md`", 1)[1]
    copy = re.search(r"````markdown\n(.*?)\n````", section, re.S).group(1)
    code = (ROOT / "src/finengine/skills/spec/dynamic_run_planner.md").read_text(encoding="utf-8")

    assert copy.replace("\r\n", "\n").strip() == code.replace("\r\n", "\n").strip()


def test_the_classifier_prompt_no_longer_forbids_the_split_it_teaches() -> None:
    from finengine.skills.atoms import intent

    prompt = intent._SYSTEM_PROMPT
    block = prompt.split("СОСТАВНОЙ ЗАПРОС.", 1)[1].split("МАРШРУТ", 1)[0]

    assert "НЕ дроби" not in prompt
    assert "custom_analysis" in block and "depends_on" in block, "зависимая custom_analysis названа в блоке составных"
    assert "Без АНАЛИЗА (статистики, собственного расчёта, внешних данных) это НЕ custom_analysis" in prompt
