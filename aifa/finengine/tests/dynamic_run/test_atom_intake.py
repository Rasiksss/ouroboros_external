"""Приёмка атома (`atom_intake`): одно место проверки для автора службы и для подзадачи ouroboros.

ADR-0043 п. 6, ADR-0044 п. 1; CONTRACTS.md §3.1 (`aifa_register_atom`), C4.3–C4.4. Предел автора службы — три попытки
на запись, номера 1..3 — после перевода на приёмку остался прежним.
"""
from __future__ import annotations

import json

import pytest
from dynamic_support import MARGIN_CODE, margin_request
from test_atom_author import BAD_CODE, Script, _reply
from test_dynamic_run import ExtractScenario
from test_ephemeral_in_graph import CASE, OWNER

from finengine.contracts import GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec import atom_intake
from finengine.skills.spec.atom_author import AtomAuthor
from finengine.skills.spec.atom_intake import CodeVerdict, Registered, check_code
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target

NAME = "eph.ebitda_margin"


def session_with_inputs(base, events: list[dict], author: AtomAuthor | None = None) -> LoopSession:
    goal = GoalIR(raw_query="маржа", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell("ifrs_nf_ru")
    )
    session = LoopSession(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal, target=Target(output_type="QuoteEvidence", entity_ref="ent"),
        inputs=RunInputs(document_text="отчёт", extra={"trace_id": "trace-1"}), author=author, run_passport=passport,
        event_sink=events.append,
    )
    ExtractScenario(base, passport)("Извлеки EBITDA и выручку")
    return session


def attempts(events: list[dict]) -> list[tuple[int, str, int, str]]:
    return [(e["move"], e["atom"], e["attempt"], e["status"]) for e in events if e["type"] == "author_attempt"]


def test_the_service_author_still_has_three_attempts_per_write_numbered_one_to_three(base, local_room) -> None:
    """ADR-0043 п. 6: предел автора службы не изменился: три попытки на запись, номера 1..3."""
    events: list[dict] = []
    script = Script([_reply(BAD_CODE)] * 5)
    session = session_with_inputs(base, events, AtomAuthor(script))

    problem = session._ensure_atom(margin_request())

    assert problem is not None and "автор не написал рабочий код за 3 попытки" in problem
    assert attempts(events) == [(1, NAME, 1, "error"), (1, NAME, 2, "error"), (1, NAME, 3, "error")]
    assert len(script.replies) == 2, "четвёртой попытки автор не делал"
    assert NAME not in session.atoms


def test_an_atom_the_author_wrote_is_registered_by_the_intake(base, local_room) -> None:
    events: list[dict] = []
    session = session_with_inputs(base, events, AtomAuthor(Script([_reply(BAD_CODE), _reply()])))

    assert session._ensure_atom(margin_request()) is None

    assert session.atoms[NAME].code == MARGIN_CODE and session.created == 1
    assert attempts(events) == [(1, NAME, 1, "error"), (1, NAME, 2, "ok")]


def test_reports_of_a_subtask_and_attempts_of_the_author_never_collide_in_one_move(base, local_room) -> None:
    """ADR-0044 п. 1: один счётчик на (ход цикла, имя) — экран склеивает попытки по паре (атом, номер)."""
    events: list[dict] = []
    session = session_with_inputs(base, events, AtomAuthor(Script([_reply(BAD_CODE), _reply()])))
    session.intake.report(NAME, "error", "не сошлось", "", move=1)

    session._ensure_atom(margin_request())

    assert [a[2] for a in attempts(events)] == [1, 2, 3]


def test_the_author_and_a_subtask_check_code_in_the_same_place(base, local_room, monkeypatch: pytest.MonkeyPatch) -> None:
    """Правило шага 16: ни `atom_author.py`, ни роут не проверяют код сами — только `atom_intake.check_code`."""
    seen: list[str] = []

    def spy(request, code, inputs, **kwargs) -> CodeVerdict:
        seen.append(request.name)
        return check_code(request, code, inputs, **kwargs)

    monkeypatch.setattr(atom_intake, "check_code", spy)
    monkeypatch.setattr("finengine.skills.spec.atom_author.check_code", spy)
    session = session_with_inputs(base, [], AtomAuthor(Script([_reply()])))

    assert session._ensure_atom(margin_request()) is None
    outcome = session.register_atom(margin_request(name="eph.second_margin"), MARGIN_CODE)

    assert isinstance(outcome, Registered)
    assert seen == [NAME, "eph.second_margin"]


def test_the_last_started_move_is_the_running_one_and_otherwise_the_previous_one(base) -> None:
    session = session_with_inputs(base, [])

    assert session.last_started_move == 1, "до первого хода — первый"
    session.turns = 3
    assert session.last_started_move == 3
    session._moving = True
    assert session.last_started_move == 4


def test_the_counter_survives_the_save_and_the_restore_of_the_state(base) -> None:
    from finengine.skills.spec.loop_state import restore_state, state_of

    session = session_with_inputs(base, [])
    session.intake.report(NAME, "error", "x", "")
    session.intake.report(NAME, "ok", None, "")
    saved = json.loads(json.dumps(state_of(session)))

    other = session_with_inputs(base, [])
    restore_state(other, saved)

    assert other.intake.log.number(1, NAME) == 3
