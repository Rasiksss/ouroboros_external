from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from starlette.responses import JSONResponse

from ouroboros.agent.loop_tool_execution import ToolCallExecutor
from ouroboros.storage.settings.schema import EgressAuth, MTLSAuth, NoAuth
from tests.helpers.a2a_credentials import (
    AGENT,
    ALICE,
    ALICE_SESSION,
    ALICE_SUDIR,
    ALICE_TOKEN,
    HEADERS_AUTH,
    STATIC,
    TOKEN_AUTH,
    credential_dependencies,
    fake_crm_agent,
    recorded_contexts,
    run_connected,
    stored_tokens,
)
from tests.helpers.egress_auth import reported_task_handle
from tests.helpers.parametrize import parametrize_from_dict
from tests.test_helper.a2a import build_fake_agent, install_fake_transport
from tests.test_helper.a2a.scripted_agent import ScriptedAgent

SECRETS = (STATIC, ALICE_TOKEN, ALICE_SUDIR, ALICE_SESSION)
SDK_TEXT = ("HTTP Error", "Client error", "Unauthorized", "Forbidden", "denied", "fake-a2a.test")
_KINDS: dict[str, dict[str, Any]] = {
    "none": {"auth": NoAuth(), "hint": ("an operator must change", "security_config/a2a.py")},
    "mtls": {"auth": MTLSAuth(), "hint": ("an operator must change", "security_config/a2a.py")},
    "user_headers": {"auth": HEADERS_AUTH, "hint": ("browser session", "sign in again")},
    "user_token": {
        "auth": TOKEN_AUTH,
        "hint": (f"personal token for A2A agent {AGENT}", "owner settings", "fix the static token"),
    },
}


class _RefusingMiddleware:
    def __init__(self, app: Any, *, status: int, answered_before_refusing: int) -> None:
        self.app = app
        self.status = status
        self.answered = answered_before_refusing

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or "/.well-known/" in str(scope.get("path") or ""):
            await self.app(scope, receive, send)
            return
        if self.answered > 0:
            self.answered -= 1
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "denied"}, status_code=self.status)
        await response(scope, receive, send)


class _CancelRefusingMiddleware:
    def __init__(self, app: Any, *, agent: ScriptedAgent, status: int) -> None:
        self.app = app
        self.agent = agent
        self.status = status

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http" and self.agent.requests[-1].rpc_method == "CancelTask":
            await JSONResponse({"error": "denied"}, status_code=self.status)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _refusing_agent(monkeypatch, *, status: int, answered_before_refusing: int) -> ScriptedAgent:
    agent = build_fake_agent(AGENT)
    agent.app = _RefusingMiddleware(agent.app, status=status, answered_before_refusing=answered_before_refusing)
    install_fake_transport(monkeypatch, agent)
    return agent


def _run(agent: ScriptedAgent, auth: EgressAuth, body):
    return run_connected(
        agent, auth, credential_dependencies(contexts=recorded_contexts(), tokens=stored_tokens()), body
    )


def _assert_addressed(result: str, hint: tuple[str, ...]) -> None:
    assert result.startswith(f"⚠️ A2A_ERROR ({AGENT}):"), result
    for fragment in hint:
        assert fragment in result
    for fragment in (*SDK_TEXT, *SECRETS):
        assert fragment not in result
    assert ToolCallExecutor._is_tool_execution_failure(True, result, f"a2a__{AGENT}") is True


@parametrize_from_dict(_KINDS)
@pytest.mark.parametrize("status", [401, 403])
def test_http_401_or_403_is_addressed_by_auth_kind(auth, hint, status, monkeypatch) -> None:
    agent = _refusing_agent(monkeypatch, status=status, answered_before_refusing=0)

    result = _run(agent, auth, lambda connection: connection.call(ALICE, "hello", timeout_sec=10.0))

    _assert_addressed(result, hint)
    assert agent.completed == []


@parametrize_from_dict(
    {
        "status": {"follow": lambda connection, handle: connection.status(ALICE, handle, timeout_sec=10.0)},
        "cancel": {"follow": lambda connection, handle: connection.cancel(ALICE, handle, timeout_sec=10.0)},
    }
)
def test_a_rejected_follow_up_is_addressed_the_same_way(follow, monkeypatch) -> None:
    agent = _refusing_agent(monkeypatch, status=403, answered_before_refusing=1)

    async def body(connection) -> tuple[str, str]:
        handle = reported_task_handle(await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0))
        return handle, await follow(connection, handle)

    handle, result = _run(agent, _KINDS["user_token"]["auth"], body)

    _assert_addressed(result, _KINDS["user_token"]["hint"])
    assert handle in result


def test_a_rejected_poll_is_addressed_the_same_way(monkeypatch) -> None:
    agent = _refusing_agent(monkeypatch, status=401, answered_before_refusing=1)

    result = _run(
        agent, _KINDS["user_token"]["auth"], lambda connection: connection.call(ALICE, "slow:5 long", timeout_sec=10.0)
    )

    _assert_addressed(result, _KINDS["user_token"]["hint"])
    assert "reading task" in result


@parametrize_from_dict(_KINDS)
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_cancel_after_the_deadline_keeps_the_timeout_and_the_handle(auth, hint, status, monkeypatch) -> None:
    agent = build_fake_agent(AGENT)
    agent.app.app = _CancelRefusingMiddleware(agent.app.app, agent=agent, status=status)
    install_fake_transport(monkeypatch, agent)

    result = _run(agent, auth, lambda connection: connection.call(ALICE, "slow:5 long", timeout_sec=0.4))

    assert result.startswith(f"⚠️ A2A_TIMEOUT ({AGENT}):"), result
    assert "did not authorize the cancel" in result
    assert f"task_id={reported_task_handle(result)}" in result
    for fragment in hint:
        assert fragment in result
    for fragment in (*SDK_TEXT, *SECRETS):
        assert fragment not in result
    assert [request.rpc_method for request in agent.requests].count("CancelTask") == 1


@parametrize_from_dict(_KINDS)
def test_auth_required_is_addressed_by_auth_kind_and_keeps_the_task(auth, hint, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)

    async def body(connection) -> tuple[str, str]:
        result = await connection.call(ALICE, "auth please", timeout_sec=10.0)
        return result, await connection.status(ALICE, reported_task_handle(result), timeout_sec=10.0)

    result, state = _run(agent, auth, body)

    _assert_addressed(result, hint)
    assert "auth-required" in result
    assert "canceled" not in state
    assert [request for request in agent.requests if request.rpc_method == "CancelTask"] == []
