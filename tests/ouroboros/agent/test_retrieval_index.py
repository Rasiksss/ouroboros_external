from __future__ import annotations

from typing import Any

from ouroboros.agent.retrieval_index import sanitize_retrieval_mapping


def _nested(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": "x"}
    for _ in range(depth):
        value = {"k": value}
    return value


def test_sanitize_retrieval_mapping_marks_depth_limit_without_rewrapping():
    assert sanitize_retrieval_mapping("t", {"data": _nested(5)}) == {
        "data": {"k": {"k": {"k": {"k": {"_depth_limit": True}}}}}
    }


def test_sanitize_retrieval_mapping_redacts_secret_key_parts_below_the_root():
    assert sanitize_retrieval_mapping(
        "t",
        {"outer": {"user_credential": "c-1", "items": [{"credential_ref": "c-2", "title": "ok"}]}},
    ) == {
        "outer": {
            "user_credential": "*** REDACTED ***",
            "items": [{"credential_ref": "*** REDACTED ***", "title": "ok"}],
        }
    }
