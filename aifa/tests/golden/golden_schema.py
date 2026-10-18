"""Схемы событий экрана, выведенные из опорных записей (F5 (а), ADR-0005, ADR-0015).

Схема описывает событие так, как оно идёт по `GET /chat/runs/{id}/events` (конверт целиком), а не
после `unwrapRunEvents`: так её применяет и тест записей, и контрактный тест нового хода. Ключ типа
— `legacy.<legacy_event.type>` для конверта `legacy`, иначе `event_type`. Схема ловит то, что
сломает экран: пропавшее или переименованное поле, другой тип значения, новый тип события. Лишнее
поле не мешает экрану и допускается; статусы (`completed.status`) — любая строка, потому что запись
видит не все исходы (A11).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from golden_format import FORMAT
from jsonschema import Draft202012Validator

JsonObject = dict[str, Any]

DRAFT: str = Draft202012Validator.META_SCHEMA["$schema"]
SUFFIX = ".schema.json"
# Биения — не записи журнала (`golden_record.py`): у них схемы нет и нарушением они не считаются.
HEARTBEATS = frozenset({"heartbeat", "ping"})
# Различители, где одно значение — часть контракта: смена версии конверта или типа ломает разбор.
PINNED = (("event_type",), ("schema_version",), ("legacy_event", "type"))


class SchemaError(ValueError):
    """Файл не содержит событий или набор схем повреждён."""


def event_key(item: Mapping[str, Any]) -> str:
    kind = item.get("event_type")
    if kind == "legacy":
        return f"legacy.{item.get('legacy_event', {}).get('type', '?')}"
    if kind is None:
        return f"screen.{item.get('type', '?')}"
    return str(kind)


def schema_filename(key: str) -> str:
    return f"{key}{SUFFIX}"


def read_events(path: Path) -> list[JsonObject]:
    """События из опорной записи (`turns[].events`) или файла прогона (`events`, как в
    `aifa/finengine/tests/dynamic_run/live/runs`)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("format") == FORMAT:
        return [event for turn in raw["turns"] for event in turn["events"]]
    if isinstance(raw, dict) and isinstance(raw.get("events"), list):
        return [event for event in raw["events"] if isinstance(event, dict)]
    raise SchemaError(f"в {path.name} нет событий: ни записи {FORMAT}, ни поля `events`")


def derive(events: Iterable[Mapping[str, Any]]) -> dict[str, JsonObject]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if event.get("event_type") in HEARTBEATS:
            continue
        groups.setdefault(event_key(event), []).append(event)
    return {key: _event_schema(key, samples) for key, samples in sorted(groups.items())}


def _event_schema(key: str, samples: Sequence[Mapping[str, Any]]) -> JsonObject:
    schema = _infer(list(samples))
    for path in PINNED:
        _pin(schema, path, samples)
    return {
        "$schema": DRAFT,
        "title": key,
        "x-samples": len(samples),
        **schema,
    }


def _pin(schema: JsonObject, path: tuple[str, ...], samples: Sequence[Mapping[str, Any]]) -> None:
    values = {_dig(sample, path) for sample in samples}
    if len(values) != 1 or None in values:
        return
    node = schema
    for step in path[:-1]:
        node = node["properties"][step]
    node["properties"][path[-1]] = {"const": values.pop()}


def _dig(sample: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = sample
    for step in path:
        if not isinstance(value, Mapping) or step not in value:
            return None
        value = value[step]
    return value


def _infer(values: list[Any]) -> JsonObject:
    by_kind: dict[str, list[Any]] = {}
    for value in values:
        by_kind.setdefault(_kind(value), []).append(value)
    pieces: list[JsonObject] = []
    for kind in ("null", "boolean", "string"):
        if kind in by_kind:
            pieces.append({"type": kind})
    if "number" in by_kind:
        pieces.append({"type": "number"})
    elif "integer" in by_kind:
        pieces.append({"type": "integer"})
    if "array" in by_kind:
        pieces.append(_array_schema(by_kind["array"]))
    if "object" in by_kind:
        pieces.append(_object_schema(by_kind["object"]))
    return pieces[0] if len(pieces) == 1 else {"anyOf": pieces}


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _array_schema(arrays: list[list[Any]]) -> JsonObject:
    items = [item for array in arrays for item in array]
    return {"type": "array", "items": _infer(items)} if items else {"type": "array"}


def _object_schema(objects: list[dict[str, Any]]) -> JsonObject:
    keys = sorted({key for obj in objects for key in obj})
    schema: JsonObject = {
        "type": "object",
        "properties": {key: _infer([obj[key] for obj in objects if key in obj]) for key in keys},
    }
    required = [key for key in keys if all(key in obj for obj in objects)]
    if required:
        schema["required"] = required
    return schema


def write_schemas(schemas: Mapping[str, JsonObject], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for key, schema in schemas.items():
        text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (directory / schema_filename(key)).write_text(text, encoding="utf-8")


def load_schemas(directory: Path) -> dict[str, JsonObject]:
    schemas: dict[str, JsonObject] = {}
    for path in sorted(directory.glob(f"*{SUFFIX}")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name.removesuffix(SUFFIX)] = schema
    if not schemas:
        raise SchemaError(f"в {directory} нет схем `*{SUFFIX}`")
    return schemas


def violations(event: Mapping[str, Any], schemas: Mapping[str, JsonObject]) -> list[str]:
    if event.get("event_type") in HEARTBEATS:
        return []
    key = event_key(event)
    if key not in schemas:
        return [f"нет схемы для типа `{key}`"]
    validator = Draft202012Validator(schemas[key])
    return [
        f"{key}: {'/'.join(str(step) for step in error.absolute_path) or '<событие>'}: "
        f"{error.message}"
        for error in sorted(validator.iter_errors(event), key=lambda error: list(error.path))
    ]
