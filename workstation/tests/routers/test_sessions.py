"""Session lifecycle API tests."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.repositories.audit_event import AuditEventRepository
from app.repositories.session import SessionRepository
from app.repositories.workspace_persistence import WorkspacePersistenceRepository
from app.routers.v1.session.dependencies import get_session_service
from app.schemas.session import SessionCreateRequest
from app.services.file_gateway import FileGateway
from app.services.policy_engine import PolicyEngine
from app.services.runtime import RuntimeAdapter
from app.services.session.factory import build_session_service
from app.services.session.service import SessionService
from app.services.workspace_persistence import WorkspacePersistenceService


def _auth_headers(session_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Session-Id": session_id,
    }


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


async def test_create_session_returns_token_and_defaults(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-alpha"},
        json={},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "session-alpha"
    assert body["status"] == "ready"
    assert isinstance(body["token"], str)
    assert body["quotas"]["maxFileBytes"] > 0
    rootfs_path = Path(os.environ["ROOTFS_ROOT"]) / "session-alpha"
    assert (rootfs_path / ".template-marker").read_text(encoding="utf-8") == "runtime-template"


async def test_create_session_reuses_existing_active_session_without_token(client: AsyncClient) -> None:
    first = await client.post("/v1/sessions", headers={"X-Session-Id": "session-reuse"}, json={})
    assert first.status_code == 201

    second = await client.post("/v1/sessions", headers={"X-Session-Id": "session-reuse"}, json={})
    assert second.status_code == 200
    body = second.json()
    assert body["id"] == "session-reuse"
    assert "token" not in body
    _assert_utc_timestamp(body["createdAt"])
    _assert_utc_timestamp(body["expiresAt"])


async def test_get_session_requires_valid_bearer_and_matching_session_header(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "session-auth"}, json={})
    token = created.json()["token"]

    unauthorized = await client.get("/v1/sessions/session-auth")
    assert unauthorized.status_code == 401
    unauthorized_body = unauthorized.json()
    assert unauthorized_body["code"] == "auth-required"
    assert unauthorized_body["message"] == "Не передан bearer-токен или неверный формат Authorization"
    assert unauthorized_body["details"]["reason"] == "missing-or-malformed-bearer"

    missing_session_header = await client.get(
        "/v1/sessions/session-auth",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert missing_session_header.status_code == 401
    missing_session_header_body = missing_session_header.json()
    assert missing_session_header_body["code"] == "auth-required"
    assert missing_session_header_body["message"] == "Не передан X-Session-Id"
    assert missing_session_header_body["details"]["reason"] == "missing-session-id"

    mismatch = await client.get(
        "/v1/sessions/session-auth",
        headers=_auth_headers("other-session", token),
    )
    assert mismatch.status_code == 401
    mismatch_body = mismatch.json()
    assert mismatch_body["code"] == "auth-required"
    assert mismatch_body["message"] == "X-Session-Id не совпадает с session_id в path"
    assert mismatch_body["details"]["reason"] == "session-id-mismatch"

    wrong_token = await client.get(
        "/v1/sessions/session-auth",
        headers=_auth_headers("session-auth", "wrong-token"),
    )
    assert wrong_token.status_code == 401
    wrong_token_body = wrong_token.json()
    assert wrong_token_body["code"] == "auth-required"
    assert wrong_token_body["message"] == "Bearer-токен не соответствует сессии"
    assert wrong_token_body["details"]["reason"] == "token-mismatch"

    ok = await client.get(
        "/v1/sessions/session-auth",
        headers=_auth_headers("session-auth", token),
    )
    assert ok.status_code == 200
    assert ok.json()["id"] == "session-auth"


async def test_get_session_returns_specific_not_found_message(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/sessions/session-missing",
        headers=_auth_headers("session-missing", "token-from-nowhere"),
    )

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not-found"
    assert body["message"] == "Сессия не найдена"
    assert body["details"]["sessionId"] == "session-missing"


async def test_terminate_session_is_idempotent(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "session-term"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("session-term", token)
    rootfs_path = Path(os.environ["ROOTFS_ROOT"]) / "session-term"

    first = await client.post("/v1/sessions/session-term:terminate", headers=headers)
    second = await client.post("/v1/sessions/session-term:terminate", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "terminated"
    assert second.json()["status"] == "terminated"
    assert "terminatedAt" in first.json()
    _assert_utc_timestamp(first.json()["terminatedAt"])
    assert not rootfs_path.exists()


async def test_snapshot_failure_keeps_rootfs_and_ready_status(client: AsyncClient, app, session) -> None:
    class FailingStore:
        enabled = True

        async def put_blob(self, sha256: str, content: bytes) -> str:
            raise RuntimeError("s3 write failed")

        async def put_manifest(self, *, scope_id: str, revision_id: str, manifest: dict) -> str:
            raise RuntimeError("unexpected manifest write")

        async def put_latest(self, *, scope_id: str, manifest: dict) -> str:
            raise RuntimeError("unexpected latest write")

        async def get_latest(self, scope_id: str) -> dict | None:
            return None

        async def get_json(self, object_key: str) -> dict:
            raise RuntimeError("unexpected manifest read")

        async def get_blob(self, object_key: str) -> bytes:
            raise RuntimeError("unexpected blob read")

    def _service_override() -> SessionService:
        return SessionService(
            session_repository=SessionRepository(session),
            audit_repository=AuditEventRepository(session),
            runtime=RuntimeAdapter(),
            policy=PolicyEngine(),
            file_gateway=FileGateway(),
            workspace_persistence=WorkspacePersistenceService(
                repository=WorkspacePersistenceRepository(session),
                object_store=FailingStore(),
            ),
        )

    app.dependency_overrides[get_session_service] = _service_override
    try:
        created = await client.post(
            "/v1/sessions",
            headers={"X-Session-Id": "session-snapshot-fail"},
            json={"workspacePersistence": {"scopeId": "scope-fail", "root": "/workspace"}},
        )
        assert created.status_code == 201
        token = created.json()["token"]
        rootfs_path = Path(os.environ["ROOTFS_ROOT"]) / "session-snapshot-fail"
        (rootfs_path / "workspace").mkdir(exist_ok=True)
        (rootfs_path / "workspace" / "file.txt").write_text("must survive", encoding="utf-8")

        terminated = await client.post(
            "/v1/sessions/session-snapshot-fail:terminate",
            headers=_auth_headers("session-snapshot-fail", token),
        )

        assert terminated.status_code == 500
        assert rootfs_path.exists()
        stored = await SessionRepository(session).get_by_id("session-snapshot-fail")
        assert stored is not None
        assert stored.status == "ready"
    finally:
        app.dependency_overrides.pop(get_session_service, None)


@pytest.mark.asyncio
async def test_terminate_session_returns_busy_during_active_exec(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "session-term-busy"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("session-term-busy", token)
    entered_exec = asyncio.Event()
    release_exec = asyncio.Event()
    original_exec = RuntimeAdapter.exec

    async def _blocked_exec(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        entered_exec.set()
        await release_exec.wait()
        return await original_exec(self, *args, **kwargs)

    monkeypatch.setattr(RuntimeAdapter, "exec", _blocked_exec)
    exec_task = asyncio.create_task(
        client.post(
            "/v1/sessions/session-term-busy/exec",
            headers=headers,
            json={"command": [sys.executable, "-c", "print('done')"]},
        )
    )
    await asyncio.wait_for(entered_exec.wait(), timeout=2)

    terminated = await client.post("/v1/sessions/session-term-busy:terminate", headers=headers)
    release_exec.set()
    exec_response = await exec_task

    assert terminated.status_code == 409
    assert terminated.json()["code"] == "invalid-state"
    assert terminated.json()["message"] == "Сессия занята другой операцией"
    assert exec_response.status_code == 200


async def test_session_reads_keep_utc_timestamps_after_db_roundtrip(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "session-utc"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("session-utc", token)

    body = (await client.get("/v1/sessions/session-utc", headers=headers)).json()

    _assert_utc_timestamp(body["createdAt"])
    _assert_utc_timestamp(body["updatedAt"])
    _assert_utc_timestamp(body["lastUsedAt"])
    _assert_utc_timestamp(body["expiresAt"])


async def test_create_session_recreates_after_termination(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "session-recreate"}, json={})
    token = created.json()["token"]
    headers = _auth_headers("session-recreate", token)
    terminated = await client.post("/v1/sessions/session-recreate:terminate", headers=headers)
    assert terminated.status_code == 200

    recreated = await client.post("/v1/sessions", headers={"X-Session-Id": "session-recreate"}, json={})
    assert recreated.status_code == 201
    body = recreated.json()
    assert body["id"] == "session-recreate"
    assert body["status"] == "ready"
    assert isinstance(body["token"], str)
    assert body["token"] != token


async def test_create_session_rejects_ttl_above_hard_cap(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-ttl"},
        json={"idleTimeoutSec": 901},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "bad-request"


async def test_create_session_rejects_invalid_session_id(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "/tmp/escape-me"},
        json={},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "bad-request"


@pytest.mark.asyncio
async def test_concurrent_create_session_reuses_single_session(session) -> None:  # type: ignore[no-untyped-def]
    service = build_session_service(session)

    first, second = await asyncio.gather(
        service.create_session(session_id="session-race", payload=SessionCreateRequest()),
        service.create_session(session_id="session-race", payload=SessionCreateRequest()),
    )

    assert {first.created, second.created} == {True, False}
    assert first.session.id == "session-race"
    assert second.session.id == "session-race"


@pytest.mark.asyncio
async def test_failed_provisioning_persists_failed_session_and_audit(session) -> None:  # type: ignore[no-untyped-def]
    service = build_session_service(session)

    async def _fail(self, rootfs_path: str) -> None:  # type: ignore[no-untyped-def]
        Path(rootfs_path).mkdir(parents=True, exist_ok=True)
        raise RuntimeError("boom-provision")

    service._runtime = RuntimeAdapter()  # type: ignore[attr-defined]
    service._runtime.provision_rootfs = _fail.__get__(service._runtime, RuntimeAdapter)  # type: ignore[method-assign]

    with pytest.raises(Exception, match="failed to provision session runtime"):
        await service.create_session(session_id="session-failed", payload=SessionCreateRequest())

    await session.rollback()
    repository = SessionRepository(session)
    audit_repository = AuditEventRepository(session)
    stored = await repository.get_by_id("session-failed")
    events = await audit_repository.list_for_session("session-failed")

    assert stored is not None
    assert stored.status == "failed"
    assert stored.fail_reason is not None
    assert any(event.kind == "state.transition" for event in events)
    assert not Path(stored.rootfs_path).exists()


async def test_create_session_enforces_the_shared_secret(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "IS_CREATE_SESSION_SECRET", True)
    monkeypatch.setattr(settings, "SESSION_CREATE_SECRET", "shared-secret")

    missing = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-secret"},
        json={},
    )
    assert missing.status_code == 401
    assert missing.json()["details"]["reason"] == "missing-session-create-secret"

    wrong = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-secret", "X-Session-Create-Secret": "guessed"},
        json={},
    )
    assert wrong.status_code == 401
    assert wrong.json()["details"]["reason"] == "session-create-secret-mismatch"

    accepted = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-secret", "X-Session-Create-Secret": "shared-secret"},
        json={},
    )
    assert accepted.status_code == 201


async def test_create_session_ignores_the_secret_header_when_not_enforced(client: AsyncClient) -> None:
    # Default deployment: the header is inert, so a gateway that already sends it can be
    # rolled out before the station starts enforcing.
    response = await client.post(
        "/v1/sessions",
        headers={"X-Session-Id": "session-open", "X-Session-Create-Secret": "irrelevant"},
        json={},
    )

    assert response.status_code == 201


def test_enforcing_without_a_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(IS_CREATE_SESSION_SECRET=True, SESSION_CREATE_SECRET="  ")
