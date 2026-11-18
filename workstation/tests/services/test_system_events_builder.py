"""Тесты построения AGENT_SYSTEM_CONFIGURATION payload."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.session import Session, SessionStatus
from app.services.system_events.builder import (
    build_session_created_event,
    build_session_deleted_event,
    build_workstation_start_event,
)


def test_workstation_start_event_uses_metadata_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Startup-событие содержит envelope, metadata и attributes по контракту."""
    current = datetime(2026, 4, 14, 9, 15, 30, tzinfo=UTC)
    monkeypatch.setenv("AGENT_ID", "CI00001234")
    monkeypatch.setenv("CLUSTER_ID", "dev1.apps.example")
    monkeypatch.setenv("POD_NAMESPACE", "dev1")
    monkeypatch.setenv("DISTRIBUTIVE", "agent-distrib.zip")

    event = build_workstation_start_event(now=current)

    assert event == {
        "contract-version": "1.0",
        "timestamp": "2026-04-14T09:15:30Z",
        "event-type": "AGENT_SYSTEM_CONFIGURATION",
        "metadata": {
            "agent-id": "CI00001234",
            "cluster-id": "dev1.apps.example",
            "namespace": "dev1",
            "distributive": "agent-distrib.zip",
        },
        "payload": [
            {
                "config-type": "aef-workstation-start",
                "attributes": {
                    "aef-workstation-version": "0.1.0",
                },
            }
        ],
    }


def test_session_events_normalize_missing_metadata_and_optional_fields(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Session-события нормализуют пропуски и не отправляют create expires-at."""
    for env_name in ("AGENT_ID", "CLUSTER_ID", "POD_NAMESPACE", "DISTRIBUTIVE"):
        monkeypatch.delenv(env_name, raising=False)
    created_at = datetime(2026, 4, 14, 9, 15, 30, tzinfo=UTC)
    terminated_at = datetime(2026, 4, 14, 10, 15, 30, tzinfo=UTC)
    session = Session(
        id="sandbox-1",
        status=SessionStatus.TERMINATED.value,
        token_hash="token-hash",
        rootfs_path="/tmp/sandbox-1",
        created_at=created_at,
        last_used_at=created_at,
        expires_at=terminated_at,
        idle_timeout_sec=900,
        absolute_ttl_sec=3600,
        terminated_at=terminated_at,
    )

    created_event = build_session_created_event(
        session,
        total_sandbox_sessions=3,
        now=created_at,
    )
    deleted_event = build_session_deleted_event(
        session,
        total_sandbox_sessions=2,
        now=terminated_at,
    )

    assert created_event["metadata"] == {
        "agent-id": "",
        "cluster-id": "",
        "namespace": "",
        "distributive": "",
    }
    created_attributes = created_event["payload"][0]["attributes"]
    assert created_event["payload"][0]["config-type"] == "aef-workstation-create-session"
    assert created_attributes == {
        "aef-workstation-version": "0.1.0",
        "sandbox-session-id": "sandbox-1",
        "created-at": "2026-04-14T09:15:30Z",
        "total-sandbox-session": 3,
    }
    assert "expires-at" not in created_attributes

    deleted_attributes = deleted_event["payload"][0]["attributes"]
    assert deleted_event["payload"][0]["config-type"] == "aef-workstation-delete-session"
    assert deleted_attributes == {
        "aef-workstation-version": "0.1.0",
        "sandbox-session-id": "sandbox-1",
        "created-at": "2026-04-14T09:15:30Z",
        "expires-at": "2026-04-14T10:15:30Z",
        "total-sandbox-session": 2,
    }
