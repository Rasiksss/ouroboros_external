from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from infra.user_tokens.interface import IUserTokenStore, UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a import connection as a2a_connection
from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
from ouroboros.storage.settings.schema import EgressAuth, MTLSAuth, NoAuth, UserHeadersAuth, UserTokenAuth
from tests.helpers.a2a_credentials import (
    AGENT,
    ALICE,
    ALICE_SESSION,
    ALICE_SUDIR,
    ALICE_TOKEN,
    BOB,
    BOB_SESSION,
    BOB_SUDIR,
    HEADERS_AUTH,
    STATIC,
    TARGET,
    TOKEN_AUTH,
    called,
    credential_dependencies,
    entry_for,
    fake_crm_agent,
    recorded_contexts,
    run_connected,
    scope_in_another_chat,
    sent_requests,
    stored_tokens,
    unread_contexts,
)
from tests.helpers.egress_auth import a2a_egress_dependencies, connected_a2a_agent, unread_egress_auth_dependencies
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.user_tokens import FixedUserTokenStore
from tests.test_helper.a2a import build_fake_agent, install_fake_transport

SECRETS = (STATIC, ALICE_TOKEN, ALICE_SUDIR, ALICE_SESSION, BOB_SUDIR, BOB_SESSION)
THE_FAKE_REMOTE_AGENTS_OWN_LOGGERS = ("a2a.server",)


def _refused(auth: EgressAuth, scope: OwnerScope, tokens: FixedUserTokenStore, reason: str, reads: int) -> dict:
    return {"auth": auth, "scope": scope, "tokens": tokens, "reason": reason, "reads": reads}


_NO_STATIC_TOKEN = UserTokenAuth(auth_header="X-API-Key")
_REFUSED = {
    "a browser run with nothing saved": _refused(
        HEADERS_AUTH,
        scope_in_another_chat("carol", "main", SurfaceId.BROWSER),
        stored_tokens(),
        "the user credentials lack the header 'X-Sudir-Token'",
        1,
    ),
    **{
        f"user_headers from {surface.value}": _refused(
            HEADERS_AUTH,
            scope_in_another_chat("alice", "main", surface),
            stored_tokens(),
            f"user credentials are not collected on surface {surface.value}",
            0,
        )
        for surface in (SurfaceId.DIALOGS, SurfaceId.SKILL_BRIDGE, SurfaceId.A2A)
    },
    "a missing cookie": _refused(
        UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir", "theme"]),
        ALICE,
        stored_tokens(),
        "the user credentials lack the cookie 'theme'",
        1,
    ),
    "a raising store beside a static token": _refused(
        TOKEN_AUTH,
        ALICE,
        FixedUserTokenStore({}, error=RuntimeError("store down backend-secret-99")),
        "the user token store is unavailable",
        1,
    ),
    "a CR LF personal token": _refused(
        TOKEN_AUTH,
        ALICE,
        FixedUserTokenStore({(TARGET, "alice", "main"): "evil-value\r\nX-Injected: yes"}),
        "the stored user token for the header 'X-API-Key' carries characters a request header cannot hold",
        1,
    ),
    "neither a personal nor a static token": _refused(
        _NO_STATIC_TOKEN,
        scope_in_another_chat("bob", "main", SurfaceId.DIALOGS),
        stored_tokens(),
        "no user token is stored for this owner and agent and the agent declares no static auth_token",
        1,
    ),
    "the a2a surface without a static token": _refused(
        _NO_STATIC_TOKEN,
        scope_in_another_chat("alice", "main", SurfaceId.A2A),
        stored_tokens(),
        "no user token is looked up on surface a2a and the agent declares no static auth_token",
        0,
    ),
}


@parametrize_from_dict(_REFUSED)
def test_a_refused_authorization_sends_no_request_and_renders_no_credential(
    auth, scope, tokens, reason, reads, monkeypatch
) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens.loads.clear()
    contexts = recorded_contexts()
    dependencies = credential_dependencies(contexts=contexts, tokens=tokens)

    result = called(agent, auth, dependencies, scope, "hello")

    assert result == f"⚠️ A2A_ERROR ({AGENT}): cannot be authorized: {reason}"
    assert [request.rpc_method for request in agent.requests] == [""]
    assert len(contexts.loads) + len(tokens.loads) == reads
    for secret in (*SECRETS, "backend-secret-99", "evil-value", "Injected"):
        assert secret not in result


class _HangingUserTokenStore(IUserTokenStore):
    def __init__(self, ignored_cancels: int) -> None:
        self.ignored_cancels = ignored_cancels
        self.loads: list[UserTokenTarget] = []
        self.unwound: list[UserTokenTarget] = []

    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        self.loads.append(target)
        try:
            for _ in range(self.ignored_cancels):
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.Event().wait()
            await asyncio.Event().wait()
        finally:
            self.unwound.append(target)
        return None


@parametrize_from_dict(
    {
        "a store that stops when cancelled": {"ignored_cancels": 0},
        "a store that waits out a cancel": {"ignored_cancels": 1},
    }
)
def test_a_hanging_store_fails_inside_the_call_deadline_and_sends_nothing(ignored_cancels, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = _HangingUserTokenStore(ignored_cancels)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    async def scenario() -> tuple[str, float, list[UserTokenTarget]]:
        async with connected_a2a_agent(entry_for(agent, TOKEN_AUTH), dependencies) as connection:
            started = time.monotonic()
            result = await asyncio.wait_for(connection.call(ALICE, "hello", timeout_sec=0.3), timeout=2.0)
            elapsed = time.monotonic() - started
        return result, elapsed, list(tokens.unwound)

    result, elapsed, unwound = asyncio.run(scenario())

    assert result.startswith(f"⚠️ A2A_ERROR ({AGENT}):")
    assert "nothing was sent" in result
    assert "A2A_TIMEOUT" not in result
    assert "A2A_SUBMISSION_UNKNOWN" not in result
    assert elapsed < 0.4
    assert tokens.loads == unwound == [TARGET]
    assert [request.rpc_method for request in agent.requests] == [""]


def _stall_the_loop(seconds: float) -> None:
    time.sleep(seconds)


class _PastTheDeadlineUserTokenStore(IUserTokenStore):
    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        await asyncio.sleep(0.28)
        _stall_the_loop(0.05)
        return ALICE_TOKEN


@parametrize_from_dict(
    {
        "call": {"operate": lambda connection, handle: connection.call(ALICE, "hello", timeout_sec=0.3)},
        "status": {"operate": lambda connection, handle: connection.status(ALICE, handle, timeout_sec=0.3)},
        "cancel": {"operate": lambda connection, handle: connection.cancel(ALICE, handle, timeout_sec=0.3)},
    }
)
def test_credentials_resolved_past_the_deadline_send_nothing(operate, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=_PastTheDeadlineUserTokenStore())
    handle = dependencies.task_handles.seal(ALICE, AGENT, "remote-task-1")

    result = run_connected(agent, TOKEN_AUTH, dependencies, lambda connection: operate(connection, handle))

    assert result == f"⚠️ A2A_ERROR ({AGENT}): the credentials were not resolved within 0.3s, so nothing was sent"
    assert [request.rpc_method for request in agent.requests] == [""]


def test_a_call_cancelled_while_its_credentials_resolve_leaves_no_store_read_running(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = _HangingUserTokenStore(0)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    async def body(connection) -> list[UserTokenTarget]:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(connection.call(ALICE, "hello", timeout_sec=10.0), timeout=0.2)
        await asyncio.sleep(0)
        return list(tokens.unwound)

    unwound = run_connected(agent, TOKEN_AUTH, dependencies, body)

    assert unwound == [TARGET]
    assert [request.rpc_method for request in agent.requests] == [""]


class _NeverStoppingUserTokenStore(IUserTokenStore):
    def __init__(self, unwinds_after_sec: float) -> None:
        self._unwinds_after_sec = unwinds_after_sec

    async def load(self, target: UserTokenTarget, owner_id: str, agent_id: str) -> str | None:
        deadline = time.monotonic() + self._unwinds_after_sec
        while (remaining := deadline - time.monotonic()) > 0:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(remaining)
        return None


def test_closing_gives_up_on_a_resolution_that_outlives_every_cancel(monkeypatch, caplog) -> None:
    monkeypatch.setattr(a2a_connection, "_ABANDONED_RESOLUTION_CANCEL_BUDGET_SEC", 0.1)
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=_NeverStoppingUserTokenStore(0.6))

    async def scenario() -> tuple[str, float]:
        connection = A2aRemoteAgentConnection(entry_for(agent, TOKEN_AUTH), dependencies)
        await connection.connect()
        result = await connection.call(ALICE, "hello", timeout_sec=0.1)
        started = time.monotonic()
        await asyncio.wait_for(connection.aclose(), timeout=10.0)
        return result, time.monotonic() - started

    with caplog.at_level(logging.WARNING):
        result, elapsed = asyncio.run(scenario())

    assert "nothing was sent" in result
    assert elapsed < 0.4
    assert [record.getMessage() for record in caplog.records if "ignored cancels" in record.getMessage()] != []


@parametrize_from_dict({"none": {"auth": NoAuth()}, "mtls": {"auth": MTLSAuth()}})
def test_an_agent_without_user_authorization_reads_no_store(auth, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    dependencies = a2a_egress_dependencies(unread_egress_auth_dependencies())

    result = called(agent, auth, dependencies, scope_in_another_chat("dave", "main", SurfaceId.DIALOGS), "hello")

    (send,) = sent_requests(agent, "SendMessage")
    assert "answer: hello" in result
    assert {"x-api-key", "x-sudir-token", "cookie", "authorization"}.isdisjoint(send.headers)


def test_a_run_on_the_a2a_surface_sends_the_static_token_and_reads_no_store(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = stored_tokens()
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    called(agent, TOKEN_AUTH, dependencies, scope_in_another_chat("alice", "main", SurfaceId.A2A), "hello")

    (send,) = sent_requests(agent, "SendMessage")
    assert send.headers["x-api-key"] == STATIC
    assert tokens.loads == []


@parametrize_from_dict({"dialogs": {"surface": SurfaceId.DIALOGS}, "skill bridge": {"surface": SurfaceId.SKILL_BRIDGE}})
def test_dialogs_and_skill_bridge_runs_look_up_their_own_personal_token(surface, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = FixedUserTokenStore({(TARGET, "bob", "helper"): "bob-personal-21"})
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    called(agent, TOKEN_AUTH, dependencies, scope_in_another_chat("bob", "helper", surface), "hello")

    (send,) = sent_requests(agent, "SendMessage")
    assert send.headers["x-api-key"] == "bob-personal-21"
    assert tokens.loads == [(TARGET, "bob", "helper")]


def test_an_mcp_token_with_the_same_id_is_not_sent(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = FixedUserTokenStore(
        {(UserTokenTarget(UserTokenTargetKind.MCP_SERVER, AGENT), "alice", "main"): ALICE_TOKEN}
    )

    called(agent, TOKEN_AUTH, credential_dependencies(contexts=unread_contexts(), tokens=tokens), ALICE, "hello")

    (send,) = sent_requests(agent, "SendMessage")
    assert send.headers["x-api-key"] == STATIC
    assert tokens.loads == [(TARGET, "alice", "main")]
    assert all(ALICE_TOKEN not in value for request in agent.requests for value in request.headers.values())


def test_no_log_record_contains_a_credential(monkeypatch, caplog) -> None:
    by_headers = build_fake_agent("hr")
    by_token = build_fake_agent(AGENT)
    guarded = build_fake_agent("guarded", accepted_tokens=frozenset({"never-sent"}))
    lacking_cookie = build_fake_agent("hr-theme")
    guarded_headers = build_fake_agent("hr-guarded", accepted_tokens=frozenset({"never-sent"}))
    install_fake_transport(monkeypatch, by_headers, by_token, guarded, lacking_cookie, guarded_headers)
    tokens = FixedUserTokenStore(
        {
            (TARGET, "alice", "main"): ALICE_TOKEN,
            (UserTokenTarget(UserTokenTargetKind.A2A_AGENT, "guarded"), "alice", "main"): ALICE_TOKEN,
        }
    )
    dependencies = credential_dependencies(contexts=recorded_contexts(), tokens=tokens)
    a2a_run = scope_in_another_chat("alice", "main", SurfaceId.A2A)

    async def scenario() -> list[str]:
        async with (
            connected_a2a_agent(entry_for(by_headers, HEADERS_AUTH), dependencies) as headers_connection,
            connected_a2a_agent(entry_for(by_token, TOKEN_AUTH), dependencies) as token_connection,
            connected_a2a_agent(entry_for(guarded, TOKEN_AUTH), dependencies) as guarded_connection,
            connected_a2a_agent(
                entry_for(lacking_cookie, UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir", "theme"])),
                dependencies,
            ) as lacking_cookie_connection,
            connected_a2a_agent(entry_for(guarded_headers, HEADERS_AUTH), dependencies) as guarded_headers_connection,
        ):
            return [
                await headers_connection.call(ALICE, "slow:0.2 polled", timeout_sec=10.0),
                await headers_connection.call(BOB, "slow:5 forever", timeout_sec=0.3),
                await headers_connection.call(a2a_run, "refused", timeout_sec=10.0),
                await token_connection.call(ALICE, "slow:0.2 personal", timeout_sec=10.0),
                await token_connection.call(BOB, "slow:5 forever", timeout_sec=0.3),
                await guarded_connection.call(ALICE, "rejected", timeout_sec=10.0),
                await lacking_cookie_connection.call(ALICE, "refused", timeout_sec=10.0),
                await guarded_headers_connection.call(ALICE, "rejected", timeout_sec=10.0),
            ]

    with caplog.at_level(logging.DEBUG):
        results = asyncio.run(scenario())

    formatter = logging.Formatter()
    rendered = [
        record.getMessage() + (formatter.formatException(record.exc_info) if record.exc_info else "")
        for record in caplog.records
        if not record.name.startswith(THE_FAKE_REMOTE_AGENTS_OWN_LOGGERS)
    ]
    assert [result.split(" ", 2)[1] for result in results[1:3] + results[4:]] == [
        "A2A_TIMEOUT",
        "A2A_ERROR",
        "A2A_TIMEOUT",
        "A2A_ERROR",
        "A2A_ERROR",
        "A2A_ERROR",
    ]
    assert "lack the cookie 'theme'" in results[6]
    assert "sign in again" in results[7]
    assert [request.rpc_method for request in lacking_cookie.requests] == [""]
    assert any(request.headers.get("x-sudir-token") == ALICE_SUDIR for request in guarded_headers.requests)
    assert any("guarded" in text for text in rendered)
    for text in rendered + results:
        for secret in SECRETS:
            assert secret not in text
