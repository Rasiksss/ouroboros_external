"""Exec API tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.db import get_session
from app.repositories.audit_event import AuditEventRepository
from app.services.runtime.adapter import RuntimeAdapter


def _auth_headers(session_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Session-Id": session_id,
    }


async def _wait_for_paths(*paths: Path) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2
    while loop.time() < deadline:
        if all(path.exists() for path in paths):
            return
        await asyncio.sleep(0.02)
    missing = [str(path) for path in paths if not path.exists()]
    raise AssertionError(f"paths were not created in time: {missing}")


@contextlib.asynccontextmanager
async def _client_with_request_sessions(app: FastAPI, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    previous_override = app.dependency_overrides.get(get_session)

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    app.dependency_overrides[get_session] = _override_get_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous_override


@pytest.mark.asyncio
async def test_exec_returns_sync_result(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-sync"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-sync/exec",
        headers=_auth_headers("exec-sync", token),
        json={"command": [sys.executable, "-c", "print('ok')"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exitCode"] == 0
    assert body["stdout"] == "ok\n"
    assert body["stderr"] == ""
    assert body["timedOut"] is False
    assert body["durationMs"] >= 0


@pytest.mark.asyncio
async def test_exec_audit_records_local_wrapper_runtime(client: AsyncClient, session) -> None:  # type: ignore[no-untyped-def]
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-audit"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-audit/exec",
        headers=_auth_headers("exec-audit", token),
        json={"command": [sys.executable, "-c", "print('ok')"]},
    )

    assert response.status_code == 200
    events = await AuditEventRepository(session).list_for_session("exec-audit")
    request_payload = next(event.payload for event in events if event.kind == "exec.request")
    response_payload = next(event.payload for event in events if event.kind == "exec.response")
    assert request_payload["runtime"] == "local"
    assert request_payload["wrapper"] == "fake_sandbox"
    assert response_payload["runtime"] == "local"
    assert response_payload["wrapper"] == "fake_sandbox"
    assert response_payload["landlock"] == "applied"
    assert response_payload["seccomp"] == "applied"
    assert response_payload["cgroup"] == "applied"


@pytest.mark.asyncio
async def test_exec_accepts_bare_allowlisted_command(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-bare"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-bare/exec",
        headers=_auth_headers("exec-bare", token),
        json={"command": ["bash", "-lc", "echo bare-ok"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exitCode"] == 0
    assert body["stdout"] == "bare-ok\n"


@pytest.mark.asyncio
async def test_exec_rejects_unknown_bare_command(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-bare-unknown"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-bare-unknown/exec",
        headers=_auth_headers("exec-bare-unknown", token),
        json={"command": ["definitely-not-allowlisted"]},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad-request"
    assert body["message"] == "command executable is not in the local exec allowlist"
    assert body["details"]["command"] == "definitely-not-allowlisted"


@pytest.mark.asyncio
async def test_exec_rejects_denied_command(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-policy"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-policy/exec",
        headers=_auth_headers("exec-policy", token),
        json={"command": ["sudo", "true"]},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "policy-deny"
    assert body["message"] == "command 'sudo' is denied"
    assert body["details"]["token"] == "sudo"


@pytest.mark.asyncio
async def test_exec_rejects_denied_token_inside_shell_payload(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-policy-shell"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-policy-shell/exec",
        headers=_auth_headers("exec-policy-shell", token),
        json={"command": ["/bin/bash", "-lc", "echo ok && sudo true"]},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "policy-deny"
    assert body["message"] == "command 'sudo' is denied"
    assert body["details"]["token"] == "sudo"


@pytest.mark.asyncio
async def test_exec_timeout_returns_504(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-timeout"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-timeout/exec",
        headers=_auth_headers("exec-timeout", token),
        json={"command": [sys.executable, "-c", "import time; time.sleep(1.2)"], "timeoutSec": 1},
    )

    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "timeout"
    assert body["message"] == "Команда превысила timeout выполнения"


@pytest.mark.asyncio
async def test_exec_stream_returns_ndjson_exit(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-stream"}, json={})
    token = created.json()["token"]

    async with client.stream(
        "POST",
        "/v1/sessions/exec-stream/exec:stream",
        headers=_auth_headers("exec-stream", token),
        json={
            "command": [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ],
        },
    ) as response:
        assert response.status_code == 200
        body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode("utf-8")

    events = [json.loads(line) for line in body.splitlines()]
    assert any(event["kind"] == "stdout" and "out" in event["data"] for event in events)
    assert any(event["kind"] == "stderr" and "err" in event["data"] for event in events)
    assert events[-1]["kind"] == "exit"
    assert events[-1]["exitCode"] == 0
    assert events[-1]["timedOut"] is False


@pytest.mark.asyncio
async def test_exec_stream_returns_policy_error_before_stream_starts(client: AsyncClient) -> None:
    created = await client.post("/v1/sessions", headers={"X-Session-Id": "exec-stream-deny"}, json={})
    token = created.json()["token"]

    response = await client.post(
        "/v1/sessions/exec-stream-deny/exec:stream",
        headers=_auth_headers("exec-stream-deny", token),
        json={"command": ["sudo", "true"]},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "policy-deny"


@pytest.mark.asyncio
async def test_parallel_exec_runs_concurrently_and_succeeds(app: FastAPI, engine: AsyncEngine) -> None:
    session_id = "exec-parallel"
    async with _client_with_request_sessions(app, engine) as client:
        created = await client.post("/v1/sessions", headers={"X-Session-Id": session_id}, json={})
        token = created.json()["token"]
        headers = _auth_headers(session_id, token)
        workspace = Path(os.environ["ROOTFS_ROOT"]) / session_id / "workspace"
        first_script = (
            "import pathlib, time; pathlib.Path('one.started').write_text('1'); time.sleep(0.5); print('one')"
        )
        second_script = (
            "import pathlib, time; pathlib.Path('two.started').write_text('1'); time.sleep(0.5); print('two')"
        )
        first = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/exec",
                headers=headers,
                json={
                    "command": [
                        sys.executable,
                        "-c",
                        first_script,
                    ],
                    "cwd": "/workspace",
                },
            )
        )
        second = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/exec",
                headers=headers,
                json={
                    "command": [
                        sys.executable,
                        "-c",
                        second_script,
                    ],
                    "cwd": "/workspace",
                },
            )
        )
        await _wait_for_paths(workspace / "one.started", workspace / "two.started")
        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["stdout"] == "one\n"
    assert second_response.json()["stdout"] == "two\n"


@pytest.mark.asyncio
async def test_parallel_exec_uses_isolated_env_files(
    app: FastAPI,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "exec-env-race"
    first_ready_to_spawn = asyncio.Event()
    second_ready_to_spawn = asyncio.Event()
    release_first_spawn = asyncio.Event()
    original_spawn = RuntimeAdapter._spawn_process
    spawn_calls = 0

    async def _spawn_with_first_exec_paused(self, *, process, stdin):  # type: ignore[no-untyped-def]
        nonlocal spawn_calls
        spawn_calls += 1
        if spawn_calls == 1:
            first_ready_to_spawn.set()
            await second_ready_to_spawn.wait()
            await release_first_spawn.wait()
        else:
            second_ready_to_spawn.set()
        return await original_spawn(self, process=process, stdin=stdin)

    monkeypatch.setattr(RuntimeAdapter, "_spawn_process", _spawn_with_first_exec_paused)

    async with _client_with_request_sessions(app, engine) as client:
        created = await client.post("/v1/sessions", headers={"X-Session-Id": session_id}, json={})
        token = created.json()["token"]
        headers = _auth_headers(session_id, token)
        first = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/exec",
                headers=headers,
                json={
                    "command": [sys.executable, "-c", "import os; print(os.environ['OPENCLAW_SHELL'])"],
                    "env": {"OPENCLAW_SHELL": "first"},
                },
            )
        )
        await asyncio.wait_for(first_ready_to_spawn.wait(), timeout=2)
        second = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/exec",
                headers=headers,
                json={
                    "command": [sys.executable, "-c", "import os; print(os.environ['OPENCLAW_SHELL'])"],
                    "env": {"OPENCLAW_SHELL": "second"},
                },
            )
        )
        try:
            await asyncio.wait_for(second_ready_to_spawn.wait(), timeout=2)
        finally:
            second_ready_to_spawn.set()
            release_first_spawn.set()
        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["stdout"] == "first\n"
    assert second_response.json()["stdout"] == "second\n"


@pytest.mark.asyncio
async def test_exec_stream_can_run_parallel_with_exec(
    app: FastAPI,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "exec-stream-parallel"
    stream_active = asyncio.Event()
    release_stream = asyncio.Event()
    original_stream_exec = RuntimeAdapter.stream_exec

    async def _blocked_stream_exec(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        async for event in original_stream_exec(self, *args, **kwargs):
            yield event
            if event.kind == "stdout":
                stream_active.set()
                await release_stream.wait()

    monkeypatch.setattr(RuntimeAdapter, "stream_exec", _blocked_stream_exec)

    async with _client_with_request_sessions(app, engine) as client:
        created = await client.post("/v1/sessions", headers={"X-Session-Id": session_id}, json={})
        token = created.json()["token"]
        headers = _auth_headers(session_id, token)
        stream_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/exec:stream",
                headers=headers,
                json={"command": [sys.executable, "-c", "print('stream-ready', flush=True)"]},
            )
        )
        await asyncio.wait_for(stream_active.wait(), timeout=2)
        exec_response = await client.post(
            f"/v1/sessions/{session_id}/exec",
            headers=headers,
            json={"command": [sys.executable, "-c", "print('exec-ok')"]},
        )
        release_stream.set()
        stream_response = await stream_task

    assert exec_response.status_code == 200
    assert exec_response.json()["stdout"] == "exec-ok\n"
    assert stream_response.status_code == 200
    events = [json.loads(line) for line in stream_response.text.splitlines()]
    assert any(event["kind"] == "stdout" and "stream-ready" in event["data"] for event in events)
    assert events[-1]["kind"] == "exit"
    assert events[-1]["exitCode"] == 0
