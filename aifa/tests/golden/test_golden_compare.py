"""Сравнение двух записей (F5 (б), C1.2): равны или человекочитаемый список различий.

Разрешённые различия несут номер ADR и не ломают равенство: `thinking` и нарезка `text_delta`
(ADR-0015), отказ при загрузке XLS/DOC (ADR-0026).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from golden_compare import compare
from golden_format import DocumentRef, FileRef, Meta, Recording, TurnRecord


def legacy(seq: int, **event: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "sequence": seq,
        "occurred_at": "2026-10-16T10:00:00Z",
        "event_type": "legacy",
        "legacy_event": event,
    }


def completed(seq: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "sequence": seq,
        "occurred_at": "2026-10-16T10:00:00Z",
        "event_type": "completed",
        "status": "succeeded",
    }


def turn(
    *events: Mapping[str, Any], answer: str = "Ответ.", files: tuple[FileRef, ...] = ()
) -> TurnRecord:
    raw = tuple(legacy(i, **e) for i, e in enumerate(events, start=1))
    return TurnRecord(
        user="вопрос",
        events=(*raw, completed(len(raw) + 1)),
        answer=answer,
        files=files,
    )


def recording(
    *turns: TurnRecord,
    documents: tuple[DocumentRef, ...] = (),
    snapshot: dict[str, Any] | None = None,
) -> Recording:
    meta = Meta(scenario="S", stand="x", build="b", recorded_at="2026-10-16T10:00:00Z")
    return Recording(meta, documents, turns, snapshot or {"case": "нет"})


ANSWER = {"type": "text_delta", "text": "Ответ."}


def test_two_recordings_of_the_same_scenario_are_equal() -> None:
    first = recording(turn({"type": "session_id", "session_id": str(uuid.uuid4())}, ANSWER))
    second = recording(turn({"type": "session_id", "session_id": str(uuid.uuid4())}, ANSWER))
    result = compare(first, second)
    assert result.equal
    assert "равны" in result.report()


def test_a_changed_field_is_reported_with_event_field_was_and_became() -> None:
    first = recording(turn({"type": "tool_call", "name": "extract"}, ANSWER))
    second = recording(turn({"type": "tool_call", "name": "calc"}, ANSWER))
    result = compare(first, second)
    assert not result.equal
    [diff] = result.differences
    assert (diff.event_type, diff.field, diff.was, diff.became) == (
        "tool_call",
        "name",
        "extract",
        "calc",
    )
    assert "ход 1" in result.report() and "tool_call" in result.report()
    assert "extract" in result.report() and "calc" in result.report()


def test_a_missing_event_is_reported_as_missing_not_as_a_cascade() -> None:
    first = recording(
        turn(
            {"type": "tool_call", "name": "a"},
            {"type": "file", "file_id": "f", "filename": "x"},
            ANSWER,
        )
    )
    second = recording(turn({"type": "tool_call", "name": "a"}, ANSWER))
    result = compare(first, second)
    assert [d.event_type for d in result.differences] == ["file"]
    assert result.differences[0].became is None


def test_an_extra_event_is_reported() -> None:
    first = recording(turn(ANSWER))
    second = recording(turn({"type": "clarify", "question": "q", "options": []}, ANSWER))
    result = compare(first, second)
    assert [d.event_type for d in result.differences] == ["clarify"]
    assert result.differences[0].was is None


def test_the_answer_text_is_compared_exactly_by_default() -> None:
    first = recording(turn({"type": "text_delta", "text": "Один."}, answer="Один."))
    second = recording(turn({"type": "text_delta", "text": "Два."}, answer="Два."))
    result = compare(first, second)
    assert not result.equal
    assert {d.event_type for d in result.differences} == {"text_delta", "answer"}


def test_presence_mode_ignores_the_wording_of_a_live_model_but_not_an_empty_answer() -> None:
    first = recording(turn({"type": "text_delta", "text": "Один."}, answer="Один."))
    second = recording(turn({"type": "text_delta", "text": "Два."}, answer="Два."))
    assert compare(first, second, text="presence").equal
    empty = recording(turn(answer=""))
    assert not compare(first, empty, text="presence").equal


def test_a_missing_thinking_is_an_allowed_difference_with_its_adr() -> None:
    old = recording(turn({"type": "thinking", "text": "Думаю"}, ANSWER))
    new = recording(turn(ANSWER))
    result = compare(old, new)
    assert result.equal
    [allowed] = result.allowed
    assert (allowed.event_type, allowed.adr) == ("thinking", "ADR-0015")
    assert "ADR-0015" in result.report()


def test_a_different_chunking_of_the_answer_is_an_allowed_difference() -> None:
    old = recording(
        turn({"type": "text_delta", "text": "От"}, {"type": "text_delta", "text": "вет."})
    )
    new = recording(turn(ANSWER))
    result = compare(old, new)
    assert result.equal
    assert [(d.event_type, d.adr) for d in result.allowed] == [("text_delta", "ADR-0015")]


def refused(name: str, status: int) -> DocumentRef:
    return DocumentRef(name=name, sha256="aa" * 32, size=3, upload_status=status)


def test_a_refused_xls_upload_on_the_new_side_is_an_allowed_difference() -> None:
    old = recording(documents=(refused("t.xls", 200),))
    new = recording(documents=(refused("t.xls", 415),))
    result = compare(old, new)
    assert result.equal
    assert [d.adr for d in result.allowed] == ["ADR-0026"]


def test_a_refused_pdf_or_xlsx_upload_is_not_allowed() -> None:
    for name in ("t.pdf", "t.xlsx"):
        old = recording(documents=(refused(name, 200),))
        new = recording(documents=(refused(name, 415),))
        assert not compare(old, new).equal, name


def test_a_different_document_hash_is_a_difference() -> None:
    old = recording(documents=(refused("t.pdf", 200),))
    new = recording(documents=(replace(refused("t.pdf", 200), sha256="bb" * 32),))
    [diff] = compare(old, new).differences
    assert diff.field == "sha256"


def test_a_missing_result_file_is_a_difference() -> None:
    xlsx = FileRef(name="r.xlsx", size=10, sha256="cc" * 32)
    old = recording(turn(ANSWER, files=(xlsx,)))
    new = recording(turn(ANSWER))
    result = compare(old, new)
    assert not result.equal
    assert "r.xlsx" in result.report()


def test_a_different_number_of_turns_is_a_difference() -> None:
    result = compare(recording(turn(ANSWER), turn(ANSWER)), recording(turn(ANSWER)))
    assert not result.equal
    assert "ходов" in result.report()


def test_snapshot_ids_do_not_matter_but_snapshot_values_do() -> None:
    def snap(ident: str, value: int) -> dict[str, Any]:
        return {"case": "нет", "artifacts": {"artifacts": [{"run_id": ident, "count": value}]}}

    same = compare(
        recording(snapshot=snap(str(uuid.uuid4()), 1)),
        recording(snapshot=snap(str(uuid.uuid4()), 1)),
    )
    assert same.equal
    other = compare(
        recording(snapshot=snap(str(uuid.uuid4()), 1)),
        recording(snapshot=snap(str(uuid.uuid4()), 2)),
    )
    assert not other.equal
