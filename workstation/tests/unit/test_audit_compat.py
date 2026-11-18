from __future__ import annotations

import pytest

from app.repositories.audit_event import _audit_code_for_event
from app.utils.audit import AUDIT_FAIL_CODE, AUDIT_SUCCESS_CODE


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("exec.denied", {}),
        ("ocr.timeout", {}),
        ("pipeline.error", {}),
        ("session.updated", {"status": "failed"}),
        ("session.updated", {"state": "timed_out"}),
        ("session.updated", {"errorMessage": "boom"}),
    ],
)
def test_failure_events_map_to_failure_code(kind: str, payload: dict[str, object]) -> None:
    assert _audit_code_for_event(kind, payload) == AUDIT_FAIL_CODE


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("session.created", {}),
        ("exec.completed", {"status": "success"}),
        ("artifact.created", {"result": "ok"}),
        ("workspace.snapshot", None),
    ],
)
def test_success_events_map_to_success_code(kind: str, payload: dict[str, object] | None) -> None:
    assert _audit_code_for_event(kind, payload) == AUDIT_SUCCESS_CODE
