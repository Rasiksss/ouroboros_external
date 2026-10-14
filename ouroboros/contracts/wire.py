"""The type-generic half of a seam codec: what every wire format needs and no seam owns.

Two seams serialize dataclass payloads across a process boundary — the transport seam
(``transport/codec.py``) and the agent-event seam (``ouroboros/agent_events/codec.py``) —
and both need the same four decisions:

* an unknown key is dropped rather than refused, because instances are upgraded one at a
  time and a rolling deploy must not become dropped messages;
* a decoded field that is not an instance of its declared type is refused rather than
  delivered, because a wire ``"false"`` and a string where a list is declared are wrong
  objects rather than failures;
* a free-form mapping carries only what a wire format carries, checked at the hand-over
  where the call site still names the producer;
* containers are copied at both ends rather than aliased.

Here rather than in either codec: a second copy of a fail-closed type check is where the
two drift, and the drift is silent — the copy that accepts what the other refuses delivers
a wrong object. The dependency also runs the right way, since ``ouroboros/contracts/`` is
the declared home for shapes that cross module boundaries and a codec below the boundary
layer may not import one above it.

Nothing here names a payload type. Each codec keeps its own knowledge of which fields are
reconstructed and which are free-form.
"""

from __future__ import annotations

import math
import types
import typing
import weakref
from collections.abc import Mapping
from dataclasses import fields
from typing import Any

#: Resolved field types, keyed **weakly** by the payload type. A strong cache here keeps
#: every type it ever saw alive, and a type that cannot die stays in its base's
#: ``__subclasses__()`` for the life of the process: a kind declared inside a test then
#: shows up in another module's set of wire kinds, and which module ran first decides
#: whether the suite passes.
_DECLARED_TYPES: weakref.WeakKeyDictionary[Any, dict[str, tuple[type, ...]]] = weakref.WeakKeyDictionary()


def copied(value: Any) -> Any:
    """Free-form containers are copied, never aliased, at both ends of the wire.

    Encoding must not hand a backend the caller's own dict: a backend stamping a
    header into the encoded mapping would be mutating a payload somebody else
    already published. Decoding must not leave the payload pointing into the
    wire buffer for the same reason in reverse.
    """
    if isinstance(value, Mapping):
        return {key: copied(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copied(item) for item in value]
    return value


def known_fields(payload_type: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The payload's entries this build of the type knows, unknown keys dropped.

    Instances are upgraded one at a time, so an older reader routinely sees a
    field a newer publisher added; refusing it would turn a rolling deploy into
    dropped user messages. A known key that is absent is simply not passed, which
    is what makes it take its declared default.
    """
    declared = {field.name for field in fields(payload_type)}
    return {key: copied(value) for key, value in payload.items() if key in declared}


def declared_types(payload_type: Any) -> dict[str, tuple[type, ...]]:
    """The runtime classes each field of ``payload_type`` may hold; memoized weakly.

    Weakly because the memo must not decide what exists: a payload type is reachable from
    its base's ``__subclasses__()``, which several call sites read as "every kind on this
    seam", so a cache holding the last reference would make a dead type immortal.
    """
    memoized = _DECLARED_TYPES.get(payload_type)
    if memoized is not None:
        return memoized
    resolved: dict[str, tuple[type, ...]] = {}
    hints = typing.get_type_hints(payload_type)
    for field in fields(payload_type):
        classes = runtime_classes(hints[field.name])
        if classes:
            resolved[field.name] = classes
    _DECLARED_TYPES[payload_type] = resolved
    return resolved


def runtime_classes(hint: Any) -> tuple[type, ...]:
    """``int | str | None`` -> ``(int, str, NoneType)``; ``list[str]`` -> ``(list,)``."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return tuple(cls for arg in typing.get_args(hint) for cls in runtime_classes(arg))
    if origin is not None:
        return (origin,) if isinstance(origin, type) else ()
    return (hint,) if isinstance(hint, type) else ()


def check_declared_types(payload_type: Any, decoded: dict[str, Any]) -> None:
    """Refuse a decoded field that is not an instance of its declared type.

    The reconstructed types are only the loudest case: a wire ``"false"`` where
    ``is_progress`` is declared ``bool`` would make every answer a progress tick,
    and a string where a list is declared iterates as characters. Both are wrong
    objects rather than failures, on the one hop where nobody is watching.
    """
    for name, classes in declared_types(payload_type).items():
        if name not in decoded:
            continue
        value = decoded[name]
        if isinstance(value, classes) and not (classes == (int,) and isinstance(value, bool)):
            continue
        expected = " | ".join(cls.__name__ for cls in classes)
        raise TypeError(f"{payload_type.__name__}.{name} decodes to {type(value).__name__}, not {expected}")


def check_json_safe(value: Any, path: str) -> None:
    """Walk one free-form container, naming the first value no wire format carries."""
    if isinstance(value, float) and not math.isfinite(value):
        # json.dumps writes a bare NaN/Infinity, which is not JSON, and NaN is
        # not even equal to itself — so the payload would fail the seam's own
        # equality promise on the far side.
        raise TypeError(f"{path} holds the non-finite float {value!r}, which no wire format carries")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} has a non-string key of type {type(key).__name__}: {key!r}")
            check_json_safe(item, f"{path}[{key!r}]")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            check_json_safe(item, f"{path}[{index}]")
        return
    raise TypeError(f"{path} holds {type(value).__name__}, which no wire format carries")


__all__ = [
    "check_declared_types",
    "check_json_safe",
    "copied",
    "declared_types",
    "known_fields",
    "runtime_classes",
]
