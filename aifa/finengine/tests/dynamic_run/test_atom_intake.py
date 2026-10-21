"""Приёмка атома (`atom_intake`): одно место проверки кода, который сдаёт подзадача ouroboros.

ADR-0043 п. 6, ADR-0044 п. 1, ADR-0054; CONTRACTS.md §3.1 (`aifa_register_atom`, `aifa_move`), C4.3–C4.4. Автора в службе нет:
код в цикл попадает только через `LoopSession.register_atom`, атом с кодом в `new_atoms` отклоняется.
"""
from __future__ import annotations

import json

import pytest
from dynamic_support import BAD_CODE, MARGIN_CODE, margin_request
from test_dynamic_run import ExtractScenario
from test_ephemeral_in_graph import CASE, OWNER

from finengine.contracts import GoalIR
from finengine.skills.atoms.run_passport import build_run_passport, classification_from_cell
from finengine.skills.spec import atom_intake
from finengine.skills.spec.atom_intake import CODE_BY_SUBTASK, CodeVerdict, Refused, Registered, check_code
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.target import Target

NAME = "eph.ebitda_margin"


def session_with_inputs(base, events: list[dict]) -> LoopSession:
    goal = GoalIR(raw_query="маржа", intent_kind="document_qa")
    passport = build_run_passport(
        "financial report", goal_ir_id=goal.goal_id, classification=classification_from_cell("ifrs_nf_ru")
    )
    session = LoopSession(
        runtime=base, owner_id=OWNER, case_id=CASE, goal=goal, target=Target(output_type="QuoteEvidence", entity_ref="ent"),
        inputs=RunInputs(document_text="отчёт", extra={"trace_id": "trace-1"}), run_passport=passport,
        event_sink=events.append,
    )
    ExtractScenario(base, passport)("Извлеки EBITDA и выручку")
    return session


def attempts(events: list[dict]) -> list[tuple[int, str, int, str]]:
    return [(e["move"], e["atom"], e["attempt"], e["status"]) for e in events if e["type"] == "author_attempt"]


def test_three_failed_registrations_of_a_name_are_numbered_one_to_three_and_the_fourth_is_refused(base, local_room) -> None:
    """ADR-0043 п. 6: три неудачные сдачи одного имени за ход цикла, номера 1..3, затем `лимит_попыток`."""
    events: list[dict] = []
    session = session_with_inputs(base, events)

    outcomes = [session.register_atom(margin_request(), BAD_CODE) for _ in range(4)]

    assert [o.code for o in outcomes if isinstance(o, Refused)] == ["неверные_аргументы"] * 3 + ["лимит_попыток"]
    assert "осталось попыток: 0" in outcomes[2].why
    assert attempts(events) == [(1, NAME, 1, "error"), (1, NAME, 2, "error"), (1, NAME, 3, "error")]
    assert NAME not in session.atoms


def test_an_atom_the_subtask_registered_is_in_the_cycle_with_its_source(base, local_room) -> None:
    events: list[dict] = []
    session = session_with_inputs(base, events)

    assert isinstance(session.register_atom(margin_request(), BAD_CODE), Refused)
    outcome = session.register_atom(margin_request(), MARGIN_CODE)

    assert isinstance(outcome, Registered) and session.atoms[NAME].code == MARGIN_CODE and session.created == 1
    assert attempts(events) == [(1, NAME, 1, "error"), (1, NAME, 2, "ok")]


def test_reports_of_a_subtask_and_its_registrations_never_collide_in_one_move(base, local_room) -> None:
    """ADR-0044 п. 1: один счётчик на (ход цикла, имя) — экран склеивает попытки по паре (атом, номер)."""
    events: list[dict] = []
    session = session_with_inputs(base, events)
    session.intake.report(NAME, "error", "не сошлось", "", move=1)

    session.register_atom(margin_request(), BAD_CODE)
    session.register_atom(margin_request(), MARGIN_CODE)

    assert [a[2] for a in attempts(events)] == [1, 2, 3]


def test_every_registration_checks_the_code_in_the_one_place(base, local_room, monkeypatch: pytest.MonkeyPatch) -> None:
    """Правило шага 16: ни роут, ни цикл не проверяют код сами — только `atom_intake.check_code`."""
    seen: list[str] = []

    def spy(request, code, inputs, **kwargs) -> CodeVerdict:
        seen.append(request.name)
        return check_code(request, code, inputs, **kwargs)

    monkeypatch.setattr(atom_intake, "check_code", spy)
    session = session_with_inputs(base, [])

    assert isinstance(session.register_atom(margin_request(), MARGIN_CODE), Registered)
    assert isinstance(session.register_atom(margin_request(name="eph.second_margin"), MARGIN_CODE), Registered)
    assert seen == [NAME, "eph.second_margin"]


def test_a_code_atom_in_new_atoms_is_refused_and_the_hint_leads_to_the_subtask(base, local_room) -> None:
    """ADR-0054 п. 4: ход с атомом-кодом в `new_atoms` не исполняется, подсказка — подзадача и `aifa_register_atom`."""
    session = session_with_inputs(base, [])

    problem = session._ensure_atom(margin_request())

    assert problem == CODE_BY_SUBTASK
    assert "подзадача" in problem and "aifa_register_atom" in problem and "new_atoms" in problem
    assert atom_intake.refusal_code(problem) == "неверные_аргументы"
    assert NAME not in session.atoms and session.created == 0


def test_a_registered_code_atom_described_again_in_new_atoms_is_not_refused(base, local_room) -> None:
    """Описание принятого атома (тот же контракт) безвредно: код уже сдан подзадачей."""
    session = session_with_inputs(base, [])
    assert isinstance(session.register_atom(margin_request(), MARGIN_CODE), Registered)

    assert session._ensure_atom(margin_request()) is None


def test_a_changed_description_of_a_registered_code_atom_is_refused_too(base, local_room) -> None:
    """Исправить атом можно только новой сдачей подзадачи: другое описание в `new_atoms` код не меняет."""
    session = session_with_inputs(base, [])
    assert isinstance(session.register_atom(margin_request(), MARGIN_CODE), Registered)

    assert session._ensure_atom(margin_request(purpose="маржа EBITDA, уточнено")) == CODE_BY_SUBTASK


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
