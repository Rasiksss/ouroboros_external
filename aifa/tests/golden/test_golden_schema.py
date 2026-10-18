"""Вывод и проверка схем событий экрана из опорных записей (F5 (а), ADR-0005, ADR-0015)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from golden_format import FORMAT
from golden_schema import (
    SchemaError,
    derive,
    event_key,
    load_schemas,
    read_events,
    schema_filename,
    violations,
    write_schemas,
)

JsonObject = dict[str, Any]


def legacy(kind: str, **fields: Any) -> JsonObject:
    return {
        "schema_version": "1.0",
        "event_id": "e-1",
        "run_id": "r-1",
        "sequence": 1,
        "occurred_at": "2026-10-16T08:26:21Z",
        "event_type": "legacy",
        "legacy_event": {"type": kind, **fields},
    }


def completed(status: str = "succeeded") -> JsonObject:
    return {
        "schema_version": "1.0",
        "event_id": "e-2",
        "run_id": "r-1",
        "sequence": 2,
        "occurred_at": "2026-10-16T08:26:22Z",
        "event_type": "completed",
        "status": status,
    }


def test_the_key_of_an_event_names_the_wire_form_not_the_screen_alias() -> None:
    """F5 (а): `legacy` — по `legacy_event.type`, прочие конверты — по `event_type`."""
    assert event_key(legacy("text_delta", text="a")) == "legacy.text_delta"
    assert event_key(completed()) == "completed"
    assert event_key({"type": "text_delta", "text": "a"}) == "screen.text_delta"


def test_a_field_present_in_every_sample_is_required_and_typed() -> None:
    schemas = derive([legacy("text_delta", text="а"), legacy("text_delta", text="б")])
    schema = schemas["legacy.text_delta"]
    inner = schema["properties"]["legacy_event"]
    assert inner["properties"]["text"] == {"type": "string"}
    assert "text" in inner["required"]
    assert violations(legacy("text_delta", text="в"), schemas) == []


def test_a_field_missing_in_some_sample_is_allowed_to_be_absent() -> None:
    first = legacy("usage", turn={"cost_usd": 0.1, "model": "m"})
    second = legacy("usage", turn={"cost_usd": 0.2})
    schemas = derive([first, second])
    turn = schemas["legacy.usage"]["properties"]["legacy_event"]["properties"]["turn"]
    assert turn["required"] == ["cost_usd"]
    assert violations(legacy("usage", turn={"cost_usd": 1.5}), schemas) == []


def test_a_missing_required_field_is_reported_with_its_path() -> None:
    """F5 (а): нарушитель — экран читает `text`, а служба перестала его слать."""
    schemas = derive([legacy("text_delta", text="а")])
    problems = violations(legacy("text_delta"), schemas)
    assert problems and "legacy_event" in problems[0] and "text" in problems[0]


def test_a_changed_type_is_reported() -> None:
    """F5 (а): нарушитель — число вместо строки."""
    schemas = derive([legacy("text_delta", text="а")])
    assert violations(legacy("text_delta", text=5), schemas)


def test_an_event_type_without_a_schema_is_reported() -> None:
    """F5 (а): новых типов нет, кроме разрешённых ADR."""
    schemas = derive([completed()])
    problems = violations(legacy("brand_new", text="а"), schemas)
    assert problems == ["нет схемы для типа `legacy.brand_new`"]


def test_the_discriminators_are_pinned_to_their_value() -> None:
    schemas = derive([legacy("text_delta", text="а")])
    properties = schemas["legacy.text_delta"]["properties"]
    assert properties["event_type"] == {"const": "legacy"}
    assert properties["schema_version"] == {"const": "1.0"}
    assert properties["legacy_event"]["properties"]["type"] == {"const": "text_delta"}
    changed = {**legacy("text_delta", text="а"), "schema_version": "2.0"}
    assert violations(changed, schemas)


def test_an_extra_field_does_not_break_the_screen_and_is_allowed() -> None:
    schemas = derive([completed()])
    assert violations({**completed(), "note": "новое поле"}, schemas) == []


def test_integer_and_float_samples_merge_into_number() -> None:
    schemas = derive([legacy("usage", cost=1), legacy("usage", cost=0.5)])
    inner = schemas["legacy.usage"]["properties"]["legacy_event"]["properties"]
    assert inner["cost"] == {"type": "number"}
    assert violations(legacy("usage", cost=7), schemas) == []


def test_null_and_array_samples_become_unions_and_item_schemas() -> None:
    schemas = derive(
        [
            legacy("sources", items=[{"n": 1}], note=None),
            legacy("sources", items=[], note="x"),
        ]
    )
    inner = schemas["legacy.sources"]["properties"]["legacy_event"]["properties"]
    assert inner["items"] == {
        "type": "array",
        "items": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    }
    assert {piece["type"] for piece in inner["note"]["anyOf"]} == {"null", "string"}
    assert violations(legacy("sources", items=[{"n": "x"}], note=None), schemas)


def test_an_empty_array_in_every_sample_accepts_anything() -> None:
    schemas = derive([legacy("sources", items=[])])
    inner = schemas["legacy.sources"]["properties"]["legacy_event"]["properties"]
    assert inner["items"] == {"type": "array"}


def test_the_derived_schema_counts_its_samples() -> None:
    schemas = derive([completed(), completed("failed")])
    assert schemas["completed"]["x-samples"] == 2


def test_the_status_of_completed_is_any_string_not_the_observed_ones() -> None:
    """ADR-0015, A11: `failed`, `cancelled`, `partial` бывают, даже если запись их не видела."""
    schemas = derive([completed("succeeded")])
    assert violations(completed("partial"), schemas) == []


def test_schemas_survive_a_round_trip_through_files(tmp_path: Path) -> None:
    schemas = derive([completed(), legacy("text_delta", text="а")])
    write_schemas(schemas, tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "completed.schema.json",
        "legacy.text_delta.schema.json",
    ]
    assert load_schemas(tmp_path) == schemas
    assert schema_filename("legacy.text_delta") == "legacy.text_delta.schema.json"


def test_writing_is_deterministic(tmp_path: Path) -> None:
    events = [legacy("text_delta", text="а"), completed()]
    write_schemas(derive(events), tmp_path / "a")
    write_schemas(derive(list(reversed(events))), tmp_path / "b")
    for path in (tmp_path / "a").iterdir():
        assert path.read_text(encoding="utf-8") == (tmp_path / "b" / path.name).read_text(
            encoding="utf-8"
        )


def test_events_are_read_from_a_recording_and_from_a_run_file(tmp_path: Path) -> None:
    recording = {
        "format": FORMAT,
        "turns": [{"events": [completed()]}, {"events": [legacy("text_delta", text="а")]}],
    }
    run_file = {"label": "A1", "events": [completed("failed")]}
    (tmp_path / "recording.json").write_text(json.dumps(recording), encoding="utf-8")
    (tmp_path / "run.json").write_text(json.dumps(run_file), encoding="utf-8")
    assert len(read_events(tmp_path / "recording.json")) == 2
    assert read_events(tmp_path / "run.json") == [completed("failed")]


def test_a_file_without_events_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "other.json").write_text('{"scenario": "A1"}', encoding="utf-8")
    with pytest.raises(SchemaError, match="other.json"):
        read_events(tmp_path / "other.json")


def test_heartbeats_are_not_part_of_a_schema_set() -> None:
    """Запись биений не хранит (`golden_record.py`): схемы для них не выводятся."""
    beat = {"event_type": "heartbeat", "schema_version": "1.0"}
    assert derive([beat, completed()]).keys() == {"completed"}
    assert violations(beat, {}) == []
