"""Формат опорной записи (ADR-0005, F5 (б)): запись → файл → запись без потерь."""

from __future__ import annotations

import json

import pytest
from golden_format import (
    FORMAT,
    DocumentRef,
    FileRef,
    Meta,
    Recording,
    RecordingError,
    TurnRecord,
    dump,
    load,
)


def sample() -> Recording:
    return Recording(
        meta=Meta(
            scenario="S0",
            stand="finagent.homyakagent.ru",
            build="abc1234",
            recorded_at="2026-10-16T10:00:00Z",
        ),
        documents=(DocumentRef(name="a.pdf", sha256="ab" * 32, size=10, upload_status=200),),
        turns=(
            TurnRecord(
                user="привет",
                events=({"event_type": "completed", "status": "succeeded"},),
                answer="здравствуйте",
                files=(FileRef(name="r.xlsx", size=3, sha256="cd" * 32),),
            ),
        ),
        snapshot={"case": "нет", "artifacts": {"artifacts": []}},
    )


def test_a_recording_survives_a_round_trip_through_its_file_text() -> None:
    recording = sample()
    assert load(dump(recording)) == recording


def test_the_file_is_readable_utf8_with_the_format_marker() -> None:
    text = dump(sample())
    assert json.loads(text)["format"] == FORMAT
    assert "здравствуйте" in text  # не \uXXXX: запись читает человек


def test_a_file_of_another_format_is_refused_with_a_reason() -> None:
    broken = json.loads(dump(sample()))
    broken["format"] = "something/9"
    with pytest.raises(RecordingError, match="формат"):
        load(json.dumps(broken))


def test_a_missing_section_is_named_in_the_error() -> None:
    broken = json.loads(dump(sample()))
    del broken["turns"]
    with pytest.raises(RecordingError, match="turns"):
        load(json.dumps(broken))


def test_a_file_that_is_not_json_is_refused_with_a_reason() -> None:
    with pytest.raises(RecordingError, match="JSON"):
        load("{not json")
