"""Custom comparison DSL: one logical assert per test function.

Instead of stretching 5-10 ``assert`` statements that check individual fields
of one object, tests call a single comparison function that ignores dynamic
fields (IDs, timestamps, worker assignments) and reports the first mismatch
with a readable message.

Usage inside ``parametrize_from_dict``::

    @parametrize_from_dict({
        "success": {"result": ..., "expected": {"status": "ok", "task_type": "task"}},
        "error":   {"result": ..., "expected": {"status": "error", "error_code": "E1"}},
    })
    def test_something(result, expected):
        assert_state_subset(result, expected)
"""

from __future__ import annotations

from typing import Any


def assert_state_subset(actual: Any, expected: dict[str, Any]) -> None:
    """Assert every key in ``expected`` matches on ``actual``.

    ``actual`` can be a dict, a Pydantic model, or any object with attributes.
    Only the keys present in ``expected`` are checked — extra fields on
    ``actual`` are ignored, making this a partial-state assertion.

    If a value in ``expected`` is a dict and the corresponding actual value
    is also a dict, the comparison recurses (partial at each level).
    """
    for key, expected_val in expected.items():
        actual_val = _get(actual, key)
        if actual_val is None and key not in _keys(actual):
            raise AssertionError(f"Missing key: {key}")
        if isinstance(expected_val, dict) and _is_dict(actual_val):
            for sub_key, sub_val in expected_val.items():
                assert _get(actual_val, sub_key) == sub_val, (
                    f"Key '{key}.{sub_key}': {_get(actual_val, sub_key)!r} != {sub_val!r}"
                )
        else:
            assert actual_val == expected_val, f"Key '{key}': {actual_val!r} != {expected_val!r}"


def task_record_eq(
    actual: Any,
    expected: Any,
    *,
    ignore: tuple[str, ...] = (
        "task_id",
        "job_id",
        "created_at",
        "queued_at",
        "started_at",
        "heartbeat_at",
        "finished_at",
        "worker_id",
        "runtime_id",
        "terminal_published_at",
        "terminal_publish_claim",
        "terminal_publish_after",
        "stop_requested_at",
        "soft_notice_at",
        "attempt",
    ),
) -> bool:
    """Compare two TaskRecord-like objects ignoring dynamic fields."""
    return _model_eq(actual, expected, ignore=ignore)


def conversation_message_eq(
    actual: Any,
    expected: Any,
    *,
    ignore: tuple[str, ...] = ("message_id", "dedup_key", "created_at", "content_hash"),
) -> bool:
    """Compare two ConversationMessage-like objects ignoring dynamic fields."""
    return _model_eq(actual, expected, ignore=ignore)


def scope_eq(actual: Any, expected: Any) -> bool:
    """Compare two OwnerScope-like objects (all fields are significant)."""
    return _model_eq(actual, expected, ignore=())


def _model_eq(actual: Any, expected: Any, *, ignore: tuple[str, ...]) -> bool:
    if type(actual) is not type(expected):
        return False
    actual_dict = _as_dict(actual)
    expected_dict = _as_dict(expected)
    for key, expected_val in expected_dict.items():
        if key in ignore:
            continue
        if actual_dict.get(key) != expected_val:
            return False
    return True


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj.keys())
    if hasattr(obj, "model_dump"):
        return set(obj.model_dump().keys())
    return set(vars(obj).keys()) if hasattr(obj, "__dict__") else set()


def _is_dict(obj: Any) -> bool:
    return isinstance(obj, dict)


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return vars(obj) if hasattr(obj, "__dict__") else {}
