from __future__ import annotations

import threading
from typing import Any

import pytest

from ouroboros.egress.workstation.client import (
    ErrorCode,
    SessionStatus,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
    WorkstationTransportError,
)
from ouroboros.egress.workstation.manager import WorkstationManager
from ouroboros.egress.workstation.session import SessionRecord


def _config(**overrides: Any) -> WorkstationConfig:
    base = {
        "base_url": "https://ws.example",
        "create_secret": "",
        "session_strategy": "ephemeral",
        "idle_timeout_sec": 900,
        "absolute_ttl_sec": 3600,
        "command_timeout_sec": 300,
        "http_timeout_sec": 30.0,
        "max_attempts": 1,
        "retry_base_delay_sec": 0.0,
        "workspace_root": "/workspace",
        "access_level": "rw",
        "attachment_max_bytes": 1024,
    }
    base.update(overrides)
    return WorkstationConfig(**base)


class FakeClient:
    """Stand-in for WorkstationClient — the manager only calls these three methods."""

    def __init__(self) -> None:
        self.get_session_calls: list[tuple[str, str]] = []
        self.create_session_calls: list[str] = []
        self.closed = False
        self.get_session_result: Any = None
        self.create_session_result: Any = None

    def get_session(self, session_id: str, token: str, *, timeout_sec: float | None = None) -> Any:
        self.get_session_calls.append((session_id, token))
        result = self.get_session_result
        if isinstance(result, Exception):
            raise result
        return result

    def create_session(self, session_id: str, *, timeout_sec: float | None = None) -> Any:
        self.create_session_calls.append(session_id)
        result = self.create_session_result
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


class _SessionState:
    def __init__(self, status: SessionStatus, quotas: dict[str, Any] | None = None) -> None:
        self.status = status
        self.quotas = quotas or {}


class _CreateResult:
    def __init__(self, *, session_id: str, token: str, created: bool, restored: bool = False, quotas=None) -> None:
        self.session_id = session_id
        self.token = token
        self.created = created
        self.restored = restored
        self.quotas = quotas or {}


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch):
    mgr = WorkstationManager()
    fake = FakeClient()
    monkeypatch.setattr(mgr, "_client_for", lambda config: fake)
    return mgr, fake


def test_reuses_ready_session_without_seeding(manager, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, fake = manager
    seed_calls: list[Any] = []
    monkeypatch.setattr("ouroboros.egress.workstation.manager.seed_workspace", lambda handle: seed_calls.append(handle))
    mgr._records["conv-1"] = SessionRecord(session_id="sess-1", token="tok-1", created_at=0.0, last_used_at=0.0)
    fake.get_session_result = _SessionState(SessionStatus.READY, {"disk_mb": 100})

    handle = mgr.get_or_create(scope_key="conv-1", config=_config())

    assert handle.record.session_id == "sess-1"
    assert handle.record.quotas == {"disk_mb": 100}
    assert fake.create_session_calls == []
    assert seed_calls == []


def test_not_found_forgets_and_recreates(manager) -> None:
    mgr, fake = manager
    mgr._records["conv-1"] = SessionRecord(session_id="sess-old", token="tok-old", created_at=0.0, last_used_at=0.0)
    fake.get_session_result = WorkstationApiError(
        operation="get_session", status_code=404, code=ErrorCode.NOT_FOUND, message="gone"
    )
    fake.create_session_result = _CreateResult(session_id="sess-new", token="tok-new", created=True)

    handle = mgr.get_or_create(scope_key="conv-1", config=_config())

    assert len(fake.create_session_calls) == 1
    assert handle.record.session_id == "sess-new"
    assert "conv-1" in mgr._records
    assert mgr._records["conv-1"].session_id == "sess-new"


@pytest.mark.parametrize(
    "status",
    [SessionStatus.PROVISIONING, SessionStatus.TERMINATING, SessionStatus.TERMINATED, SessionStatus.FAILED],
)
def test_non_ready_status_forgets_and_recreates(manager, status: SessionStatus) -> None:
    mgr, fake = manager
    mgr._records["conv-1"] = SessionRecord(session_id="sess-old", token="tok-old", created_at=0.0, last_used_at=0.0)
    fake.get_session_result = _SessionState(status)
    fake.create_session_result = _CreateResult(session_id="sess-new", token="tok-new", created=True)

    handle = mgr.get_or_create(scope_key="conv-1", config=_config())

    assert handle.record.session_id == "sess-new"


def test_other_probe_error_aborts_without_creating(manager) -> None:
    mgr, fake = manager
    mgr._records["conv-1"] = SessionRecord(session_id="sess-old", token="tok-old", created_at=0.0, last_used_at=0.0)
    fake.get_session_result = WorkstationApiError(
        operation="get_session", status_code=401, code=ErrorCode.AUTH_REQUIRED, message="nope"
    )

    with pytest.raises(WorkstationApiError):
        mgr.get_or_create(scope_key="conv-1", config=_config())

    assert fake.create_session_calls == []
    # The stale record is not discarded on an unrelated failure.
    assert mgr._records["conv-1"].session_id == "sess-old"


def test_transport_error_on_probe_aborts_without_creating(manager) -> None:
    mgr, fake = manager
    mgr._records["conv-1"] = SessionRecord(session_id="sess-old", token="tok-old", created_at=0.0, last_used_at=0.0)
    fake.get_session_result = WorkstationTransportError("network is down")

    with pytest.raises(WorkstationTransportError):
        mgr.get_or_create(scope_key="conv-1", config=_config())

    assert fake.create_session_calls == []


def test_seed_workspace_called_only_on_created_not_restored(manager, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, fake = manager
    seed_calls: list[Any] = []
    reconcile_calls: list[Any] = []
    # Patch the names bound into `manager`'s own namespace at import time —
    # patching `seeding.seed_workspace` would not affect the already-bound
    # `manager.seed_workspace` reference.
    monkeypatch.setattr("ouroboros.egress.workstation.manager.seed_workspace", lambda handle: seed_calls.append(handle))
    monkeypatch.setattr(
        "ouroboros.egress.workstation.manager.reconcile_workspace", lambda handle: reconcile_calls.append(handle)
    )
    fake.create_session_result = _CreateResult(session_id="sess-1", token="tok-1", created=True, restored=False)

    mgr.get_or_create(scope_key="conv-1", config=_config())

    assert len(seed_calls) == 1
    assert reconcile_calls == []


def test_reconcile_workspace_called_when_restored(manager, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, fake = manager
    seed_calls: list[Any] = []
    reconcile_calls: list[Any] = []
    monkeypatch.setattr("ouroboros.egress.workstation.manager.seed_workspace", lambda handle: seed_calls.append(handle))
    monkeypatch.setattr(
        "ouroboros.egress.workstation.manager.reconcile_workspace", lambda handle: reconcile_calls.append(handle)
    )
    fake.create_session_result = _CreateResult(session_id="sess-1", token="tok-1", created=True, restored=True)

    mgr.get_or_create(scope_key="conv-1", config=_config())

    assert seed_calls == []
    assert len(reconcile_calls) == 1


def test_no_seeding_when_session_already_existed(manager, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, fake = manager
    seed_calls: list[Any] = []
    monkeypatch.setattr("ouroboros.egress.workstation.manager.seed_workspace", lambda handle: seed_calls.append(handle))
    fake.create_session_result = _CreateResult(session_id="sess-1", token="tok-1", created=False)

    mgr.get_or_create(scope_key="conv-1", config=_config())

    assert seed_calls == []


def test_client_for_rebuilds_on_config_change() -> None:
    mgr = WorkstationManager()
    config_a = _config()
    config_b = _config(base_url="https://other.example")

    client_a = mgr._client_for(config_a)
    client_b = mgr._client_for(config_b)

    assert client_a is not client_b
    assert isinstance(client_a, WorkstationClient)


def test_client_for_reuses_same_client_for_same_config() -> None:
    mgr = WorkstationManager()
    config = _config()

    assert mgr._client_for(config) is mgr._client_for(config)


def test_concurrent_get_or_create_for_same_scope_creates_one_session(manager) -> None:
    mgr, fake = manager
    fake.create_session_result = _CreateResult(session_id="sess-1", token="tok-1", created=True)
    # Threads that lose the race find a record already there and reuse it.
    fake.get_session_result = _SessionState(SessionStatus.READY)
    barrier = threading.Barrier(4)

    def _call() -> None:
        barrier.wait(timeout=5)
        mgr.get_or_create(scope_key="conv-shared", config=_config())

    threads = [threading.Thread(target=_call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(fake.create_session_calls) == 1


def test_reset_for_tests_clears_records_and_client() -> None:
    mgr = WorkstationManager()
    mgr._records["conv-1"] = SessionRecord(session_id="s", token="t", created_at=0.0, last_used_at=0.0)
    mgr._client_for(_config())

    mgr.reset_for_tests()

    assert mgr._records == {}
    assert mgr._client is None
