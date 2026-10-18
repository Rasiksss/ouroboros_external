"""F5 (а): события опорных записей проходят схемы `aifa/contracts/events` (ADR-0005, ADR-0015).

Схемы выводит `golden_cli.py schemas` из записей; снимок меняется только с ADR (F13). Тест нового
хода (шаг 12) применяет те же схемы к событиям новой системы: `golden_schema.violations`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from golden_schema import SUFFIX, load_schemas, read_events, violations
from jsonschema import Draft202012Validator

TESTS = Path(__file__).resolve().parents[1]
GOLDEN = TESTS / "golden"
SCHEMAS = TESTS.parent / "contracts" / "events"
# Записи сценариев: `<СЦЕНАРИЙ>/<СЦЕНАРИЙ>-run1.json`, `-run2.json` (`golden_cli.py stability`).
RECORDINGS = sorted(GOLDEN.rglob("*-run[0-9].json"))


def name(path: Path) -> str:
    return path.relative_to(GOLDEN).as_posix()


def test_f5a_there_are_recordings_and_schemas_to_check() -> None:
    """F5 (а): пустой набор записей или схем не должен проходить за «всё соответствует»."""
    assert RECORDINGS, f"в {GOLDEN} нет записей `*-run1.json`"
    assert load_schemas(SCHEMAS)


@pytest.mark.parametrize("path", RECORDINGS, ids=name)
def test_f5a_every_event_of_a_recording_passes_its_schema(path: Path) -> None:
    """F5 (а): каждое событие записи — своего типа и своей формы; типов вне схем нет."""
    schemas = load_schemas(SCHEMAS)
    problems = [
        f"событие {number}: {problem}"
        for number, event in enumerate(read_events(path), start=1)
        for problem in violations(event, schemas)
    ]
    assert not problems, f"{name(path)}:\n" + "\n".join(problems[:20])


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob(f"*{SUFFIX}")), ids=lambda p: p.name)
def test_f5a_every_schema_is_a_valid_json_schema(path: Path) -> None:
    schema = load_schemas(SCHEMAS)[path.name.removesuffix(SUFFIX)]
    Draft202012Validator.check_schema(schema)
