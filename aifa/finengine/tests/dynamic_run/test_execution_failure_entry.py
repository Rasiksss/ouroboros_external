"""Часть 14 доводки: граф собрался и упал при исполнении с ошибкой → один ``DynamicRun`` на сообщение.

Граница (Р14.1–Р14.3), доказанная по коду, — типизированная, без разбора текста:
  ошибка        — узел ``NodeRun`` в ``TERMINAL_FAILURE`` (``failed``/``blocked``/``skipped``) либо исключение
                  из ``CaseRuntime.execute`` (таймаут, бюджет вызовов, сбой исполнителя);
  «данных нет»  — узлы отработали без ошибок, у корня нет значений (``metric_not_found``, ``changes_not_found``,
                  ``canonical_pipeline_incomplete`` без упавших узлов, ``canonical_period_mismatch``).
Не ведут в цикл: неоднозначность, ``allow_dynamic=False``, состояние ``Case`` (``CasePersistenceError``),
бюджет модели (``BudgetExceeded``), остановка (``BaseException``).

Вход — настоящий ``execute_routed_goal`` на настоящем ``CaseRuntime`` с SQLite. Подставные: ``DynamicRun`` там,
где важен только вход; модель планировщика — там, где цикл идёт по-настоящему.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from finengine.contracts import GoalIR, ScalarFact
from finengine.llm.budget import BudgetExceeded
from finengine.skills import production_runtime as production
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec import graph_executor
from finengine.skills.spec.graph_executor import ExecutionBudget, SandboxViolation
from finengine.store.cases import CaseConflictError, StalePlanRunError
from tests.integration.conftest import _make_fake_locate_and_read_value_executors

import test_planning_failure_entry as entry
from test_chat_outcome_contract import SequenceModel, _arm_dynamic, _execute
from test_planning_failure_entry import _clause, _compound, _route

spy = entry.spy
budgets = entry.budgets

_TERMINAL = {"succeeded", "failed", "blocked", "skipped"}


@dataclass
class ScriptedRead:
    """Подставной ``read.value``: ``calls`` — какие концепты просили при каждом вызове.

    ``fail_first`` — концепты, которых нет в ответе первого вызова: исполнитель сам ставит такому узлу
    ``failed`` («пакетный вызов не вернул»); ``empty`` — честное «не нашлось» (успех с нулём фактов)."""

    fail_first: frozenset[str] = frozenset()
    empty: bool = False
    period: str = "6M 2026"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, items, ctx):  # noqa: ARG002
        first = not self.calls
        self.calls.append(tuple(sorted(args.concept for args in items)))
        out: dict[str, tuple] = {}
        for args in items:
            if first and args.concept in self.fail_first:
                continue
            out[args.concept] = () if self.empty else (ScalarFact(
                produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                source_ref=f"fake:{args.concept}", concept=args.concept, period_ref=self.period,
                value=100.0, unit="RUB_MLN",
            ),)
        return out

    def requested(self, concept: str) -> int:
        return sum(concept in call for call in self.calls)


@pytest.fixture
def script() -> ScriptedRead:
    return ScriptedRead()


@pytest.fixture
def read_value(script):
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0, "NET_INCOME": 1.0})
    return script, locate


def single_metric(period: str | None = None) -> GoalIR:
    return GoalIR(
        raw_query="выручка", intent_kind="extract_registered_metric", period_ref=period,
        targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
    )


def two_metrics() -> GoalIR:
    return _compound(
        _clause("goal-1", "extract_registered_metric", concept="SALES_REVENUE"),
        _clause("goal-2", "extract_registered_metric", concept="NET_INCOME"),
    )


GOALS = [pytest.param(single_metric, id="single"), pytest.param(two_metrics, id="compound")]


@pytest.fixture
def force_budget(monkeypatch):
    """Бюджет рантайма, с которым ``execute_routed_goal`` собирает контекст (настоящий исполнитель)."""
    def _set(budget: ExecutionBudget) -> None:
        real = production.prepare_production_context
        monkeypatch.setattr(
            production, "prepare_production_context", lambda **kw: real(**{**kw, "budget": budget}),
        )
    return _set


def _node_statuses(events: list[dict]) -> dict[str, str]:
    """Последний статус каждого узла по потоку ``node_run``."""
    return {
        event["node_run"]["node_key"]["atom_name"] + event["node_run"]["node_key"]["args_fingerprint"]:
        event["node_run"]["status"]
        for event in events if event.get("type") == "node_run"
    }


# ---- исходы «ошибка исполнения»: цикл получает ошибку и отвечает -------------------------------------

def _failing_node(script, force_budget, monkeypatch) -> None:  # noqa: ARG001
    script.fail_first = frozenset({"SALES_REVENUE", "NET_INCOME"})


def _timeout(script, force_budget, monkeypatch) -> None:  # noqa: ARG001
    force_budget(ExecutionBudget(max_wall_seconds=-1.0, max_retries=0))


def _call_budget(script, force_budget, monkeypatch) -> None:  # noqa: ARG001
    force_budget(ExecutionBudget(max_atom_calls=0, max_retries=0))


def _blocked_only(script, force_budget, monkeypatch) -> None:  # noqa: ARG001
    def _violation(atom):
        raise SandboxViolation("профиль песочницы не покрывает атом")

    monkeypatch.setattr(graph_executor, "validate_sandbox_dispatch", _violation)


ERRORS = [
    pytest.param(_failing_node, id="O8-node-failed"),
    pytest.param(_timeout, id="O3-wall-timeout"),
    pytest.param(_call_budget, id="O4-atom-call-budget"),
    pytest.param(_blocked_only, id="O8b-blocked-without-failed"),
]


@pytest.mark.parametrize("make_goal", GOALS)
@pytest.mark.parametrize("arm", ERRORS)
def test_an_execution_error_starts_one_dynamic_run_and_closes_the_first_graph(
    arm, make_goal, base, script, spy, budgets, force_budget, monkeypatch,
) -> None:
    arm(script, force_budget, monkeypatch)
    events: list[dict] = []

    outcome = _route(base, make_goal(), events=events)

    assert len(spy.calls) == 1, "падение собранного графа — один цикл на сообщение (Р14.1)"
    assert spy.calls[0]["deterministic_error"], "причина входа идёт планировщику"
    assert spy.calls[0]["entry_stage"] == "execution"
    assert outcome.manifest["dynamic_run"]["answer"] == "Ответ цикла"
    # Р14.4: ошибка первой попытки в чат не уходит, граф закрыт — ни один узел не «живой»
    assert not [event for event in events if event.get("type") == "typed_error"], events
    graph_nodes = [event for event in events if event.get("type") == "plan_graph"][0]["nodes"]
    statuses = _node_statuses(events)
    assert len(statuses) == len(graph_nodes) and set(statuses.values()) <= _TERMINAL, statuses


# ---- исходы «данных нет»: прежний результат, цикла нет -----------------------------------------------

@pytest.mark.parametrize("make_goal", GOALS)
def test_a_correct_execution_that_found_nothing_is_a_result_not_a_cycle(
    make_goal, base, script, spy, budgets,
) -> None:
    script.empty = True
    events: list[dict] = []

    outcome = _route(base, make_goal(), events=events)

    assert not spy.calls, "Р14.2: «данных нет» — результат"
    assert outcome.manifest["error"]["code"] in {"metric_not_found", "canonical_pipeline_incomplete"}
    assert outcome.manifest["error"]["retryable"] is False
    assert [event["error"]["code"] for event in events if event.get("type") == "typed_error"] == [
        outcome.manifest["error"]["code"],
    ], "пользователь получает прежний отказ один раз"


def test_the_exact_pre_part_14_refusal_codes_of_empty_reads_are_pinned(base, script, spy, budgets) -> None:
    """К14.6: вид отказа для одиночной цели — ``metric_not_found`` с названием показателя."""
    script.empty = True

    outcome = _route(base, single_metric())

    assert outcome.manifest["error"]["code"] == "metric_not_found"
    assert "SALES_REVENUE" in outcome.manifest["error"]["message"]


def test_a_period_that_is_not_in_the_report_stays_canonical_period_mismatch(base, script, spy, budgets) -> None:
    _route(base, single_metric())  # факты 6M 2026 теперь в Case

    outcome = _route(base, single_metric("FY 2030"))

    assert not spy.calls
    assert outcome.manifest["error"]["code"] == "canonical_period_mismatch"


# ---- ограждения --------------------------------------------------------------------------------------

@pytest.mark.parametrize("make_goal", GOALS)
@pytest.mark.parametrize("arm", ERRORS[:1] + ERRORS[3:])
def test_a_nested_scenario_gets_the_error_back_instead_of_a_cycle(
    arm, make_goal, base, script, spy, budgets, force_budget, monkeypatch,
) -> None:
    arm(script, force_budget, monkeypatch)

    outcome = _route(base, make_goal(), allow_dynamic=False)

    assert not spy.calls, "цикла внутри цикла нет (Р14.3 + Р13.3)"
    assert outcome.manifest["error"]["code"] == "canonical_pipeline_incomplete"


def test_a_failed_node_is_still_named_in_the_refusal_when_no_cycle_can_start(base, script, spy, budgets) -> None:
    """Прежнее ожидание части 7 (M3): отказ называет узел и причину, а не общую фразу."""
    script.fail_first = frozenset({"SALES_REVENUE"})

    outcome = _route(base, single_metric(), allow_dynamic=False)

    message = outcome.manifest["error"]["message"]
    assert "read.value" in message and "SALES_REVENUE" in message


@pytest.mark.parametrize("make_goal", GOALS)
def test_a_crash_inside_a_nested_scenario_still_propagates(
    make_goal, base, script, spy, budgets, force_budget,
) -> None:
    force_budget(ExecutionBudget(max_wall_seconds=-1.0, max_retries=0))

    with pytest.raises(TimeoutError):
        _route(base, make_goal(), allow_dynamic=False)

    assert not spy.calls


@pytest.mark.parametrize("raised", [
    StalePlanRunError("документ больше не активен"),
    CaseConflictError(expected=1, actual=2),
    BudgetExceeded("бюджет LLM исчерпан"),
    asyncio.CancelledError(),
    KeyboardInterrupt(),
], ids=["case-state", "case-conflict", "model-budget", "cancelled", "interrupt"])
@pytest.mark.parametrize("make_goal", GOALS)
def test_case_state_model_budget_and_stop_never_start_a_cycle(
    raised, make_goal, base, script, spy, budgets, monkeypatch,
) -> None:
    def _boom(self, **kwargs):  # noqa: ARG001
        raise raised

    monkeypatch.setattr(CaseRuntime, "execute", _boom)

    with pytest.raises(type(raised)):
        _route(base, make_goal())

    assert not spy.calls, "Р14.3: состояние Case, бюджет модели и остановка — не повод для цикла"


def test_the_model_budget_of_the_cycle_itself_still_propagates(
    base, script, budgets, force_budget, monkeypatch,
) -> None:
    class _Exhausted:
        def __init__(self, **kwargs): ...

        def run(self, *, deterministic_error):  # noqa: ARG002
            raise BudgetExceeded("бюджет LLM исчерпан")

    monkeypatch.setattr(production, "DynamicRun", _Exhausted)
    force_budget(ExecutionBudget(max_wall_seconds=-1.0, max_retries=0))

    with pytest.raises(BudgetExceeded):
        _route(base, single_metric())


def test_ambiguity_is_still_a_question_not_a_cycle(base, script, spy, budgets) -> None:
    goal = GoalIR(
        raw_query="выручка", intent_kind="extract_registered_metric",
        targets=[{"concept": "SALES_REVENUE", "output_kind": "scalar"}],
        ambiguities=[{"term": "выручка", "candidates": ["A", "B"]}],
    )

    outcome = _route(base, goal)

    assert not spy.calls
    assert outcome.manifest["error"]["code"] == "goal_needs_clarification"


# ---- К14.1: один типизированный признак, один и тот же у производителя и потребителя -------------------

@pytest.mark.parametrize(("setup", "has_errors"), [
    pytest.param(lambda s: setattr(s, "fail_first", frozenset({"SALES_REVENUE"})), True, id="atom-failed"),
    pytest.param(lambda s: setattr(s, "empty", True), False, id="atom-returned-nothing"),
])
def test_K14_1_the_executor_marks_an_error_and_an_empty_result_differently(
    setup, has_errors, base, script, budgets,
) -> None:
    setup(script)
    goal = single_metric()
    roots = production.root_from_goal(goal)
    context = production.prepare_production_context(
        owner_id=entry.OWNER, case_id="chat-case", document_id="chat-doc", document_text="financial report",
        goal_ir=goal, store=base.store,
    )

    outcome = production.execute_production_goal(context, goal, document_text="financial report", roots=roots)

    assert bool(outcome.roots[0].execution.errors) is has_errors, "признак исполнителя"
    assert (production._execution_failure(outcome, roots) is not None) is has_errors, "его читает маршрутизатор"


# ---- К14.2/К14.3, П14.1: цикл по-настоящему, первый показатель переиспользован ------------------------

def _finalize_with_metrics(prompt: str) -> dict:
    ids = [
        line.split("|", 1)[0].strip() for line in prompt.splitlines() if "| ExtractedMetric |" in line
    ]
    return {
        "objective": "ответить", "reason": "данные получены", "action": "finalize", "scenarios": [],
        "new_atoms": [], "steps": [], "answer": "Показатели получены.", "evidence_ids": ids,
    }


def test_P14_1_K14_2_K14_3_the_cycle_sees_the_error_and_reuses_the_first_metric(
    base, script, budgets, monkeypatch,
) -> None:
    script.fail_first = frozenset({"NET_INCOME"})
    planner: SequenceModel = _arm_dynamic(monkeypatch, [
        _execute({"atom": "read.value", "concept": "NET_INCOME"}),
        _finalize_with_metrics,
    ])
    events: list[dict] = []

    outcome = _route(base, two_metrics(), events=events)

    first = planner.prompts[0]
    assert "исполнение упало" in first and "NET_INCOME" in first, "К14.2: итог «нулевого хода» — ошибка узла"
    assert "| ExtractedMetric | SALES_REVENUE |" in first, "К14.2: уже полученное видно планировщику"
    assert outcome.manifest["dynamic_run"]["status"] == "finalized"
    assert script.requested("SALES_REVENUE") == 1, "К14.3: первый показатель не извлекается заново"
    assert script.requested("NET_INCOME") == 2
    assert not [event for event in events if event.get("type") == "typed_error"]
    assert outcome.manifest["dynamic_run"]["moves"][0]["objective"], "причина входа — в журнале цикла"


# ---- П14.4: бюджет времени ---------------------------------------------------------------------------

def test_P14_4_a_timeout_starts_the_cycle_and_the_cycle_is_bounded_in_time(
    base, script, budgets, force_budget, monkeypatch,
) -> None:
    """Первая попытка упала по таймауту. Цикл стартует и сам ограничен ``RunLimits.max_wall_seconds`` (1200 с):
    суммарное ожидание = первая попытка (≤ бюджет графа) + цикл (≤ лимит) + один ход в полёте."""
    clock = [0.0]
    monkeypatch.setattr("finengine.skills.spec.dynamic_run.monotonic", lambda: clock[0])

    def _planner(prompt: str) -> dict:
        if "остановлен системой" in prompt:
            return {**_finalize_with_metrics(prompt), "action": "give_up", "answer": "Время цикла вышло."}
        clock[0] += 800.0  # каждый ход «длится» 800 с
        return _execute({"atom": "read.value", "concept": "NET_INCOME"})

    planner = _arm_dynamic(monkeypatch, [_planner])
    force_budget(ExecutionBudget(max_wall_seconds=-1.0, max_retries=0))

    outcome = _route(base, single_metric())

    manifest = outcome.manifest["dynamic_run"]
    assert manifest["status"] == "gave_up" and len(planner.prompts) == 3, planner.prompts
    assert "бюджет времени" in planner.prompts[2], "«последнее слово» знает причину остановки"
    assert clock[0] == 1600.0, "два хода: после второго 1600 с > 1200 с, третий ход не начат"
