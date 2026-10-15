"""События трассы цикла для UI: решения planner и попытки автора.

Цикл отдаёт их в тот же `event_sink`, что `plan_graph`/`node_run`; UI собирает из них ленту ходов
(вкладка рядом с графом). Здесь проверяется только то, что цикл эмитит и в каком порядке.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from finengine.contracts import GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.atom_author import AtomAuthor
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.dynamic_run import DynamicRun
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA, AtomRequest, EphemeralAtom
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import InputSpec, Target
from test_atom_author import BAD_CODE, Script
from dynamic_support import assert_event_fixture
from test_dynamic_run import (
    CALC_CODE,
    CALC_SCHEMA,
    DynamicScenario,
    ExtractScenario,
    SequenceAuthor,
    _give_up,
    _move,
    _run,
)
from test_download_atom import _csv_ok, _serve
from test_ephemeral_in_graph import CASE, ENTITY, OWNER


def _of(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event["type"] == kind]


def _normalize_event_value(value):
    if isinstance(value, dict):
        return {
            key: _normalize_event_value(item)
            for key, item in value.items()
            if key not in {"created_at", "finished_at", "node_run_id", "run_id", "started_at"}
        }
    if isinstance(value, list):
        return [_normalize_event_value(item) for item in value]
    if isinstance(value, str):
        normalized = value.replace(str(Path.cwd()), "<workspace>")
        return re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            "<uuid>", normalized, flags=re.IGNORECASE,
        )
    return value


def _normalized_events(events: list[dict]) -> list[dict]:
    return [_normalize_event_value(event) for event in events]


def _read_value_without_ebitda_and_net_debt(items, ctx) -> dict:
    """EBITDA в отчёте нет — пустой кортеж; про NET_DEBT пакет промолчал — узел падает."""
    return {args.concept: () for args in items if args.concept != "NET_DEBT"}


def test_trace_events_reach_the_browser_through_the_legacy_envelope() -> None:
    from finengine.contracts.transport import envelope_legacy_event
    from finengine.skills.result_events import author_attempt_event, dynamic_move_event

    move = dynamic_move_event(move=1, status="done", action="execute", objective="цель")
    attempt = author_attempt_event(move=1, atom="eph.x", attempt=1, status="ok", code="def run(i): ...")

    for event in (move, attempt):
        wrapped = envelope_legacy_event(event, run_id="r", sequence=1)
        assert wrapped.event_type == "legacy" and wrapped.legacy_event == event


def test_rejected_reply_and_the_terminal_move_are_traced(base) -> None:
    dynamic, _ = _run(base, ["не json", _give_up("Данных нет.")])
    events: list[dict] = []
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    moves = _of(events, "dynamic_move")
    assert [(m["move"], m["status"], m["action"]) for m in moves] == [
        (1, "rejected", "rejected"), (2, "done", "give_up"),
    ]
    assert moves[0]["details"], "причина отказа видна в ленте"
    assert "Данных нет." in moves[1]["details"]


def test_execute_move_is_traced_before_and_after_it_runs(base, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    download = AtomRequest(
        name="eph.brent_csv", purpose="ряд Brent", output_type="dyn:brent_csv",
        output_schema=DOWNLOAD_SCHEMA, url="https://data.example.org/brent.csv",
    )
    dynamic, _ = _run(base, [_move(atoms=(download,), steps=({"atom": download.name},)), _give_up()])
    events: list[dict] = []
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    moves = _of(events, "dynamic_move")
    assert [(m["move"], m["status"]) for m in moves] == [(1, "running"), (1, "done"), (2, "done")]
    running, done = moves[0], moves[1]
    assert running["objective"] == "получить промежуточные данные" and running["reason"]
    assert running["new_atoms"] == [{
        "name": "eph.brent_csv", "kind": "download", "purpose": "ряд Brent",
        "output_type": "dyn:brent_csv", "url": "https://data.example.org/brent.csv",
    }]
    assert running["steps"] == [{"atom": "eph.brent_csv", "concept": None, "period_ref": None}]
    assert done["outcome"].startswith("✓") and not done["details"].startswith("✓ ✓")


def test_failed_step_graph_only_emits_progress_events(base, local_room) -> None:
    request = AtomRequest(
        name="eph.failed_step", purpose="неуспешный шаг", output_type="dyn:failed_step",
        output_schema=CALC_SCHEMA, requires=(InputSpec("dyn:seed"),),
    )
    failed = EphemeralAtom(request, "def run(inputs):\n    raise ValueError('step boom')\n")
    dynamic, _ = _run(
        base,
        [
            _move(
                scenarios=("Подготовь вход",), atoms=(request,),
                steps=({"atom": request.name},),
            ),
            _give_up(),
        ],
        scenario=DynamicScenario(base), author=SequenceAuthor([(failed, None)]),
    )
    events: list[dict] = []
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    step_events = [event for event in events if event["type"] not in {"dynamic_move", "author_attempt"}]
    assert {event["type"] for event in step_events} <= {"plan_graph", "node_run"}
    assert {event["type"] for event in step_events} == {"plan_graph", "node_run"}


def test_empty_read_failed_step_then_give_up_matches_sse_fixture(base) -> None:
    atoms = [
        replace(atom, executor=_read_value_without_ebitda_and_net_debt) if atom.name == "read.value" else atom
        for atom in base.registry.all()
    ]
    runtime = CaseRuntime(
        store=base.store, registry=AtomRegistry(atoms), policy=base.policy,
    )
    passport = build_run_passport(
        "financial report", goal_ir_id="empty-read-goal",
        classification=classification_from_cell("ifrs_nf_ru"),
    )
    dynamic, _ = _run(
        runtime,
        [
            _move(steps=({"atom": "read.value", "concept": "EBITDA"},)),
            _move(steps=({"atom": "read.value", "concept": "NET_DEBT"},)),
            _give_up("Данных недостаточно после пустого чтения и ошибки шага."),
        ],
        run_passport=passport,
    )
    dynamic.inputs = RunInputs(document_text="отчёт")
    events: list[dict] = []
    dynamic.event_sink = events.append

    dynamic.run(deterministic_error="нет пути")

    finished = [
        event["node_run"] for event in _of(events, "node_run")
        if event["node_run"]["status"] not in ("planned", "running")
    ]
    for node_run in (run for run in finished if run["status"] == "succeeded"):
        for evidence_id in node_run["produced_evidence_ids"]:
            assert runtime.store.find_evidence(OWNER, CASE, evidence_id, valid_only=True), (
                node_run["node_key"]["atom_name"], evidence_id
            )
    empty_read, failed_read = [run for run in finished if run["node_key"]["atom_name"] == "read.value"]
    assert empty_read["status"] == "succeeded" and empty_read["produced_evidence_ids"] == []
    assert failed_read["status"] == "failed"
    first_move, second_move = [
        event for event in _of(events, "dynamic_move")
        if event["status"] == "done" and event["action"] == "execute"
    ]
    assert first_move["outcome"].startswith("результатов нет: read.value")
    assert second_move["outcome"] == "ход завершён с ошибками"
    assert not _of(events, "typed_error")
    normalized = _normalized_events(events)
    rendered = json.dumps(normalized, ensure_ascii=False)
    assert "\\r" not in rendered and "Traceback" not in rendered
    assert_event_fixture("empty-read-then-give-up.json", normalized)


def test_author_attempts_are_traced_with_the_error_and_then_the_working_code(base, local_room, monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
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
    planner = Script([
        _move(scenarios=("Извлеки EBITDA и выручку",), atoms=(download,), steps=({"atom": download.name},)),
        _move(atoms=(calc,), steps=({"atom": download.name}, {"atom": calc.name})),
        _give_up(),
    ])
    author = Script([
        json.dumps({"code": BAD_CODE, "example_inputs": {}}, ensure_ascii=False),
        json.dumps({"code": CALC_CODE, "example_inputs": {}}, ensure_ascii=False),
    ])
    goal = GoalIR(raw_query="сопоставь маржу с Brent", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell("ifrs_nf_ru"),
    )
    events: list[dict] = []
    run = DynamicRun(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal,
        target=Target(output_type="dyn:margin_with_brent", entity_ref=ENTITY, period_ref="2024"),
        inputs=RunInputs(document_text="отчёт"), run_scenario=ExtractScenario(base, passport),
        complete_fn=planner, author=AtomAuthor(author), run_passport=passport, event_sink=events.append,
    )

    run.run(deterministic_error="нет производителя")

    attempts = _of(events, "author_attempt")
    assert [(a["move"], a["atom"], a["attempt"], a["status"]) for a in attempts] == [
        (2, "eph.margin_with_brent", 1, "error"), (2, "eph.margin_with_brent", 2, "ok"),
    ]
    assert "ZeroDivisionError" in attempts[0]["error"] and attempts[0]["code"] == BAD_CODE
    assert attempts[1]["error"] is None and attempts[1]["code"] == CALC_CODE
