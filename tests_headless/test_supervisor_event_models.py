from __future__ import annotations


def test_supervisor_event_rejects_missing_type() -> None:
    import pytest
    from pydantic import ValidationError

    from ouroboros.storage.runtime_models import SupervisorEvent

    with pytest.raises(ValidationError):
        SupervisorEvent.from_event_dict({"task_id": "abc"})


def test_supervisor_event_preserves_legacy_payload() -> None:
    from ouroboros.storage.runtime_models import SupervisorEvent

    event = SupervisorEvent.from_event_dict({"type": "task_done", "task_id": "abc", "duration_sec": 1.0})

    assert event.type == "task_done"
    assert event.task_id == "abc"
    assert event.payload["duration_sec"] == 1.0
