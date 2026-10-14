"""Tests for crash report lifecycle and health invariant integrity."""

import json
import types

from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository


def _crash_env(tmp_path):
    return types.SimpleNamespace(
        drive_root=tmp_path,
        drive_path=lambda rel: tmp_path / rel,
    )


def test_valid_crash_report_persists_and_emits_runtime_event(tmp_path):
    from ouroboros.agent.startup_checks import inject_crash_report

    crash_path = tmp_path / "state" / "crash_report.json"
    crash_path.parent.mkdir(parents=True)
    crash_path.write_text(
        json.dumps({"rolled_back_from": "abcdef1234567890", "tag": "v1.2.3", "ts": "2026-07-10T00:00:00Z"}),
        encoding="utf-8",
    )
    runtime_persistence.initialize()

    audit = sql_audit_event_repository()
    inject_crash_report(_crash_env(tmp_path), audit_event_repository=audit)

    assert crash_path.exists()
    assert json.loads(crash_path.read_text(encoding="utf-8"))["tag"] == "v1.2.3"
    store = runtime_persistence.current
    assert store is not None
    events = [event.metadata for event in audit.list_audit_events(log_stream="events", limit=10)]
    assert events[-1]["type"] == "crash_rollback_detected"


def test_invalid_crash_report_logs_corruption_event(tmp_path):
    """A corrupt crash report must stay visible instead of being treated as absent."""
    from ouroboros.agent.startup_checks import inject_crash_report

    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "state" / "crash_report.json").write_text("{broken", encoding="utf-8")
    runtime_persistence.initialize()
    audit = sql_audit_event_repository()
    inject_crash_report(_crash_env(tmp_path), audit_event_repository=audit)

    store = runtime_persistence.current
    assert store is not None
    events = [event.metadata for event in audit.list_audit_events(log_stream="events", limit=10)]
    assert events[-1]["type"] == "crash_report_invalid"
    assert not (tmp_path / "logs" / "events.jsonl").exists()
