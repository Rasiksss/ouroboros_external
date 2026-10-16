"""Нормализатор потока событий (F5 (б), C1.2; ADR-0005, ADR-0015).

Что экрану важно (порядок) и что нет — выведено из `use-chat-run.ts`/`applyChatEvent`; каждое
правило названо в `RULES` вместе с причиной.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from golden_normalize import RULES, IdLabels, normalize_turn

RUN = "11111111-1111-4111-8111-111111111111"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def envelope(seq: int, event_type: str, run_id: str = RUN, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "sequence": seq,
        "occurred_at": f"2026-10-16T10:00:{seq % 60:02d}.123456Z",
        "event_type": event_type,
        **fields,
    }


def legacy(seq: int, run_id: str = RUN, **event: Any) -> dict[str, Any]:
    return envelope(seq, "legacy", run_id, legacy_event=event)


def stream(*events: Mapping[str, Any], run_id: str = RUN) -> list[dict[str, Any]]:
    """Конверты по порядку: событие `{"type": ...}` — legacy, остальное — как есть."""
    out: list[dict[str, Any]] = []
    for seq, event in enumerate(events, start=1):
        if "event_type" in event:
            out.append(envelope(seq, run_id=run_id, **dict(event)))
        else:
            out.append(legacy(seq, run_id, **event))
    return out


def normalized(raw: Sequence[Mapping[str, Any]]) -> list[Any]:
    return list(normalize_turn(raw).events)


DONE = {"event_type": "completed", "status": "succeeded"}


def test_two_runs_of_the_same_turn_are_equal_despite_ids_and_times() -> None:
    body = ({"type": "text_delta", "text": "ответ"}, DONE)
    first = stream(*body, run_id="aaaaaaaa-1111-4111-8111-111111111111")
    second = stream(*body, run_id="bbbbbbbb-2222-4222-8222-222222222222")
    assert normalized(first) == normalized(second)


def test_heartbeats_thinking_and_segment_ends_are_dropped_and_counted() -> None:
    raw = stream(
        {"type": "thinking", "text": "Думаю"},
        {"event_type": "heartbeat"},
        {"type": "text_delta", "text": "да"},
        {"type": "text_segment_end"},
        DONE,
    )
    turn = normalize_turn(raw)
    assert [e["type"] for e in turn.events] == ["text_delta", "done"]
    assert turn.dropped == {"drop_thinking": 1, "drop_heartbeat": 1, "drop_segment_end": 1}


def test_consecutive_text_deltas_merge_into_one_delta() -> None:
    chunked = stream(
        {"type": "text_delta", "text": "От"},
        {"type": "text_delta", "text": "вет"},
        DONE,
    )
    whole = stream({"type": "text_delta", "text": "Ответ"}, DONE)
    assert normalized(chunked) == normalized(whole)
    assert normalize_turn(chunked).text_delta_count == 2
    assert normalize_turn(whole).text_delta_count == 1


def test_text_before_a_tool_call_is_not_the_same_as_text_after_it() -> None:
    """Экран: текст до вызова инструмента — заметка ленты, после последнего — ответ."""
    prose_first = stream(
        {"type": "text_delta", "text": "Смотрю."},
        {"type": "tool_call", "name": "t"},
        {"type": "text_delta", "text": "Итог."},
        DONE,
    )
    answer_first = stream(
        {"type": "tool_call", "name": "t"},
        {"type": "text_delta", "text": "Смотрю."},
        {"type": "text_delta", "text": "Итог."},
        DONE,
    )
    assert normalized(prose_first) != normalized(answer_first)


def test_tool_output_folds_into_its_call_so_output_order_between_tools_is_free() -> None:
    sequential = stream(
        {"type": "tool_call", "name": "a"},
        {"type": "tool_output", "name": "a", "ok": True},
        {"type": "tool_call", "name": "b"},
        {"type": "tool_output", "name": "b", "ok": False, "reason": "нет данных"},
        DONE,
    )
    parallel = stream(
        {"type": "tool_call", "name": "a"},
        {"type": "tool_call", "name": "b"},
        {"type": "tool_output", "name": "b", "ok": False, "reason": "нет данных"},
        {"type": "tool_output", "name": "a", "ok": True},
        DONE,
    )
    assert normalized(sequential) == normalized(parallel)
    calls = [e for e in normalized(sequential) if e["type"] == "tool_call"]
    assert calls[1]["output"] == {"ok": False, "reason": "нет данных"}


def test_a_failed_tool_is_not_the_same_as_a_successful_one() -> None:
    ok = stream(
        {"type": "tool_call", "name": "a"}, {"type": "tool_output", "name": "a", "ok": True}
    )
    bad = stream(
        {"type": "tool_call", "name": "a"}, {"type": "tool_output", "name": "a", "ok": False}
    )
    assert normalized(ok) != normalized(bad)


def test_a_tool_output_without_a_call_is_kept_so_the_break_is_visible() -> None:
    raw = stream({"type": "tool_output", "name": "a", "ok": True})
    assert normalized(raw) == [{"type": "tool_output", "name": "a", "ok": True}]


def node_run(seq: int, atom: str, status: str, attempt: int = 1) -> dict[str, Any]:
    key = {"atom_name": atom, "atom_version": "1", "args_fingerprint": uuid.uuid4().hex}
    return {
        "event_type": "node_run",
        "plan_graph_hash": "g" + "0" * 15,
        "node_run": {
            "node_run_id": str(uuid.uuid4()),
            "node_key": key,
            "run_id": str(uuid.uuid4()),
            "status": status,
            "attempt": attempt,
            "started_at": f"2026-10-16T10:00:0{seq}Z",
            "finished_at": f"2026-10-16T10:00:0{seq + 1}Z",
        },
    }


def test_node_runs_of_different_nodes_may_arrive_in_any_order() -> None:
    one = stream(node_run(1, "extract", "running"), node_run(2, "calc", "running"), DONE)
    other = stream(node_run(1, "calc", "running"), node_run(2, "extract", "running"), DONE)
    assert normalized(one) == normalized(other)


def test_the_order_of_states_of_one_node_is_kept() -> None:
    """Экран держит последнее состояние узла: running→succeeded и обратное — разные ходы."""
    forward = stream(node_run(1, "n", "running"), node_run(2, "n", "succeeded"), DONE)
    backward = stream(node_run(1, "n", "succeeded"), node_run(2, "n", "running"), DONE)
    assert normalized(forward) != normalized(backward)


def test_node_runs_do_not_cross_a_barrier_event() -> None:
    before = stream(node_run(1, "a", "running"), {"type": "tool_call", "name": "t"}, DONE)
    after = stream({"type": "tool_call", "name": "t"}, node_run(2, "a", "running"), DONE)
    assert normalized(before) != normalized(after)


def test_plan_steps_of_different_lanes_may_arrive_in_any_order() -> None:
    first = {"type": "plan_step", "name": "x", "index": 0, "status": "running"}
    second = {"type": "plan_step", "name": "y", "index": 0, "status": "running"}
    lanes = (
        {"type": "plan", "name": "x", "steps": ["a"]},
        {"type": "plan", "name": "y", "steps": ["a"]},
    )
    assert normalized(stream(*lanes, first, second)) == normalized(stream(*lanes, second, first))


PLAN = {"type": "plan", "name": "p", "steps": ["a", "b", "c", "d"]}


def plan_step(index: int, status: str, name: str = "p") -> dict[str, Any]:
    return {"type": "plan_step", "name": name, "index": index, "status": status}


def test_marks_of_one_step_addressed_as_last_keep_their_order() -> None:
    """Экран: `index < 0` — последний шаг линии; две отметки одного шага не независимы."""
    done_last = stream(PLAN, plan_step(3, "running"), plan_step(-1, "done"), DONE)
    running_last = stream(PLAN, plan_step(-1, "done"), plan_step(3, "running"), DONE)
    assert normalized(done_last) != normalized(running_last)


def test_a_mark_with_an_unknown_lane_goes_to_the_first_lane_and_keeps_its_order() -> None:
    """Экран: отметка линии, которой нет, адресована первой линии (`Math.max(0, findIndex)`)."""
    done_last = stream(PLAN, plan_step(0, "running"), plan_step(0, "done", name="x"), DONE)
    running_last = stream(PLAN, plan_step(0, "done", name="x"), plan_step(0, "running"), DONE)
    assert normalized(done_last) != normalized(running_last)


def test_marks_of_different_steps_of_one_lane_may_arrive_in_any_order() -> None:
    one = stream(PLAN, plan_step(0, "done"), plan_step(-1, "running"), DONE)
    other = stream(PLAN, plan_step(-1, "running"), plan_step(0, "done"), DONE)
    assert normalized(one) == normalized(other)


def test_the_same_identifier_gets_the_same_label_and_two_identifiers_two_labels() -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    raw = stream(
        {"type": "file", "file_id": first, "filename": "a.xlsx"},
        {"type": "file", "file_id": second, "filename": "b.xlsx"},
        {"type": "text_delta", "text": f"см. {first}"},
    )
    events = normalized(raw)
    assert events[0]["file_id"] == events[2]["text"].removeprefix("см. ")
    assert events[0]["file_id"] != events[1]["file_id"]
    assert not UUID_RE.search(json.dumps(events))


def test_a_swapped_reference_between_two_identifiers_is_a_difference() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    straight = stream(
        {"type": "file", "file_id": a, "filename": "x"},
        {"type": "file", "file_id": b, "filename": "y"},
    )
    again = stream(
        {"type": "file", "file_id": a, "filename": "x"},
        {"type": "file", "file_id": a, "filename": "y"},
    )
    assert normalized(straight) != normalized(again)


def typed_result(source_ref: str) -> dict[str, Any]:
    fact = {"evidence_id": str(uuid.uuid4()), "source_ref": source_ref}
    return {
        "event_type": "result",
        "delivery_status": "complete",
        "result": {"output_type": "CalculatedMetric", "metric_id": str(uuid.uuid4()), **fact},
    }


def test_a_different_source_ref_shown_in_the_card_is_a_difference() -> None:
    """Экран показывает `source_ref` в «Источники:»; бывает `formula_id` или `concept`."""
    roe = stream(typed_result("formula.roe"), DONE)
    roa = stream(typed_result("formula.roa"), DONE)
    assert normalized(roe) != normalized(roa)


def test_a_random_identifier_inside_a_source_ref_is_still_labelled() -> None:
    first = stream(typed_result(f"{uuid.uuid4()},{uuid.uuid4()}"), DONE)
    second = stream(typed_result(f"{uuid.uuid4()},{uuid.uuid4()}"), DONE)
    assert normalized(first) == normalized(second)


def test_labels_are_shared_across_turns_through_one_registry() -> None:
    session = str(uuid.uuid4())
    labels = IdLabels()
    first = normalize_turn(stream({"type": "session_id", "session_id": session}), labels).events
    second = normalize_turn(stream({"type": "session_id", "session_id": session}), labels).events
    assert first == second


def test_timestamps_durations_and_money_are_removed() -> None:
    raw = stream(
        node_run(1, "n", "succeeded"),
        {
            "type": "usage",
            "turn": {"model": "m", "tokens_in": 123, "tokens_out": 45, "cost_usd": 0.0123},
            "project_total": {"tokens_in": 999, "cost_usd": 1.5},
        },
        {"type": "dynamic_move", "move": 1, "status": "done", "elapsed_ms": 5123},
    )
    dumped = json.dumps(normalized(raw))
    assert "started_at" not in dumped
    assert "123" not in dumped and "0.0123" not in dumped
    assert "elapsed_ms" not in dumped
    usage = next(e for e in normalized(raw) if e["type"] == "usage")
    assert usage["turn"]["model"] == "m"
    assert usage["turn"]["tokens_in"] == "<n>"


def test_a_project_id_chosen_by_the_client_does_not_make_two_runs_differ() -> None:
    """Живая запись на dev: `usage.turn.project_id` — id проекта, который клиент выдумал сам."""

    def run(project: str) -> list[Any]:
        return normalized(stream({"type": "usage", "turn": {"project_id": project, "model": "m"}}))

    assert run("qa-golden-s-aaaa1111") == run("qa-golden-s-bbbb2222")


def test_progress_ticks_collapse_and_their_digits_are_masked() -> None:
    few = stream({"type": "tool_progress", "name": "t", "text": "Идёт расчёт, 12 с"}, DONE)
    many = stream(
        {"type": "tool_progress", "name": "t", "text": "Идёт расчёт, 12 с"},
        {"type": "tool_progress", "name": "t", "text": "Идёт расчёт, 27 с"},
        {"type": "tool_progress", "name": "t", "text": "Идёт расчёт, 41 с"},
        DONE,
    )
    assert normalized(few) == normalized(many)


def test_an_unknown_event_type_passes_through_so_a_new_type_shows_in_a_diff() -> None:
    raw = stream({"type": "brand_new", "x": 1})
    assert normalized(raw) == [{"type": "brand_new", "x": 1}]


def test_envelope_events_become_the_events_the_screen_sees() -> None:
    """Как `unwrapRunEvents` в `web/src/lib/api.ts`: result→typed_result, completed→done."""
    raw = stream(
        {"event_type": "result", "delivery_status": "complete", "result": {"output_type": "x"}},
        {"event_type": "error", "error": {"code": "c", "message": "m"}, "partial_results": []},
        DONE,
    )
    assert [e["type"] for e in normalized(raw)] == ["typed_result", "typed_error", "done"]
    assert normalized(raw)[0]["status"] == "complete"


def test_every_rule_says_why() -> None:
    assert RULES
    for rule in RULES.values():
        assert rule.why.strip(), rule.name
    assert all(name == rule.name for name, rule in RULES.items())


REAL = Path(__file__).resolve().parents[2] / "finengine/tests/dynamic_run/live/runs/2026-10-06"


@pytest.mark.skipif(not REAL.is_dir(), reason="нет снятых потоков службы")
def test_real_streams_of_the_service_leave_no_uuid_and_no_timestamp() -> None:
    """Связность: настоящие потоки службы (шаг 34), а не только синтетика."""
    seen = 0
    for path in sorted(REAL.glob("A*-r1.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        dumped = json.dumps(normalized(raw["events"]), ensure_ascii=False)
        assert not UUID_RE.search(dumped), path.name
        assert "occurred_at" not in dumped and "started_at" not in dumped, path.name
        seen += 1
    assert seen
