"""JSON helpers built on orjson (fast, handles datetime/UUID out of the box)."""

from __future__ import annotations

from typing import Any

import orjson


def dumps(value: Any) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_NAIVE_UTC | orjson.OPT_SERIALIZE_NUMPY)


def loads(data: bytes | str) -> Any:
    return orjson.loads(data)
