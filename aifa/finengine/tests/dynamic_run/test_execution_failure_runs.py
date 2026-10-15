"""Часть 14 доводки: полные прогоны — от обработчика хода чата до того, что видит пользователь.

Вход — настоящий ``FinengineAgent.query_stream`` → ``execute_routed_goal``; ``Case`` — SQLite. Подставные:
классификатор, модель планировщика, ``read.value`` (граница модели). Поток записывается общей фикстурой
``fixtures/sse/execution-failure-then-cycle.json``; её читает и frontend-тест (``execution-graphs.test.ts``).
"""
from __future__ import annotations


from tests.integration.conftest import _make_fake_locate_and_read_value_executors
from tests.integration.ingress_support import (  # noqa: F401
    Turn,
    gate_env,
    patched_build_registry,
    set_intent,
)

from dynamic_support import assert_event_fixture, normalized_events
from test_chat_outcome_contract import _arm_dynamic, _execute
from test_execution_failure_entry import ScriptedRead, _finalize_with_metrics

_PLAN = {"route": "plan", "targets": [], "entity_ref": None, "period_ref": None, "role": None, "ambiguities": []}
TWO_METRICS = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "unresolved": [], "goal_ref": "goal-1", "depends_on": [],
    "additional_goals": [{
        **_PLAN, "goal_ref": "goal-2", "intent_kind": "extract_registered_metric",
        "targets": [{"concept": "NET_INCOME"}], "depends_on": [], "unresolved": [],
    }],
}
SINGLE_ABSENT = {
    **_PLAN, "intent_kind": "extract_registered_metric", "targets": [{"concept": "NET_INCOME"}],
    "unresolved": [],
}


def _turn(monkeypatch, script: ScriptedRead, form: dict, replies: list | None = None):
    set_intent(monkeypatch, form)
    locate, _ = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0, "NET_INCOME": 1.0})
    patched_build_registry(monkeypatch, locate, script)
    planner = _arm_dynamic(monkeypatch, replies or [])
    turn = Turn()
    turn.arm(monkeypatch)
    return turn, planner, turn.run("покажи выручку и чистую прибыль")


def test_P14_1_K14_4_K14_5_the_user_sees_one_answer_and_the_first_graph_is_closed_as_failed(monkeypatch) -> None:
    script = ScriptedRead(fail_first=frozenset({"NET_INCOME"}))

    turn, planner, events = _turn(monkeypatch, script, TWO_METRICS, [
        _execute({"atom": "read.value", "concept": "NET_INCOME"}), _finalize_with_metrics,
    ])

    # К14.5: ошибки первой попытки в чате нет, ответ один, диспетчер не вызывался
    assert not [event for event in events if event.get("type") in {"typed_error", "error"}], events
    texts = [event["text"] for event in events if event.get("type") == "text_delta"]
    assert texts == ["Показатели получены."] and turn.dispatcher_calls == 0
    assert script.requested("SALES_REVENUE") == 1 and script.requested("NET_INCOME") == 2
    # К14.4: граф первой попытки закрыт фактами узлов (ни один не «живой») ДО первого хода цикла
    first_move = next(i for i, e in enumerate(events) if e.get("type") == "dynamic_move")
    first_graph = next(i for i, e in enumerate(events) if e.get("type") == "plan_graph")
    first_attempt_runs = [e["node_run"] for e in events[first_graph:first_move] if e.get("type") == "node_run"]
    last_status = {
        (r["node_key"]["atom_name"], r["node_key"]["args_fingerprint"]): r["status"] for r in first_attempt_runs
    }
    assert "failed" in last_status.values() and set(last_status.values()) <= {"succeeded", "failed", "blocked"}
    assert_event_fixture("execution-failure-then-cycle.json", normalized_events(events))


def test_P14_2_a_metric_that_is_not_in_the_report_keeps_the_pre_part_14_refusal(monkeypatch) -> None:
    script = ScriptedRead(empty=True)

    turn, planner, events = _turn(monkeypatch, script, SINGLE_ABSENT)

    errors = [event["error"] for event in events if event.get("type") == "typed_error"]
    assert [error["code"] for error in errors] == ["metric_not_found"], errors
    assert not planner.prompts and not [e for e in events if e.get("type") == "dynamic_move"], "цикла нет"
    assert turn.dispatcher_calls == 0 and not any(e.get("type") == "text_delta" for e in events)
