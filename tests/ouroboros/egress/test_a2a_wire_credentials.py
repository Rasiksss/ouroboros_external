from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from a2a.types.a2a_pb2 import AgentInterface
from a2a.utils.constants import TransportProtocol

from new_core.identity import SurfaceId
from new_core.tools.loop_bridge import call_on_loop_sync
from new_core.tools.providers.a2a import A2aToolProvider
from ouroboros.agent.core import _task_scope
from ouroboros.agent_events.codec import decode_agent_event, encode_agent_event
from ouroboros.agent_events.interfaces import SubtaskRequested
from ouroboros.egress.a2a.manager import A2aManager
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import EgressAuth, MTLSAuth, NoAuth
from tests.helpers.a2a_credentials import (
    AGENT,
    ALICE,
    ALICE_SESSION,
    ALICE_SUDIR,
    ALICE_TOKEN,
    BOB,
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
from tests.helpers.egress_auth import reported_task_handle
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import FixedSecurityContextStore, context_of
from tests.helpers.shared import make_tool_ctx
from tests.test_helper.a2a import build_fake_agent, install_fake_transport
from tests.test_helper.a2a.scripted_agent import RecordedRequest, SetCookieMiddleware

CALLER = "1c8f6c0a-2c2f-4f39-88a2-6a6f1b2f4c11"
EVERY_KIND: dict[str, dict[str, EgressAuth]] = {
    "none": {"auth": NoAuth()},
    "user_headers": {"auth": HEADERS_AUTH},
    "user_token": {"auth": TOKEN_AUTH},
    "mtls": {"auth": MTLSAuth()},
}


def _text(request: RecordedRequest) -> str:
    return request.rpc_params["message"]["parts"][0]["text"]


def test_a_send_carries_only_the_calling_owners_personal_token(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=stored_tokens())

    result = called(agent, TOKEN_AUTH, dependencies, ALICE, "hello")

    (send,) = sent_requests(agent, "SendMessage")
    assert "answer: hello" in result
    assert send.headers["x-api-key"] == ALICE_TOKEN


def test_recorded_browser_headers_and_cookies_ride_the_send(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=recorded_contexts(), tokens=stored_tokens())

    result = called(agent, HEADERS_AUTH, dependencies, ALICE, "hi")

    (send,) = sent_requests(agent, "SendMessage")
    assert "answer: hi" in result
    assert (send.headers["x-sudir-token"], send.headers["cookie"]) == (ALICE_SUDIR, f"sudir={ALICE_SESSION}")


def test_every_poll_carries_the_credentials_of_the_send(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = stored_tokens()
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    result = called(agent, TOKEN_AUTH, dependencies, ALICE, "slow:0.4 crunch")

    polls = sent_requests(agent, "GetTask")
    assert "answer: slow:0.4 crunch" in result
    assert len(polls) >= 2
    assert {request.headers["x-api-key"] for request in sent_requests(agent, "SendMessage") + polls} == {ALICE_TOKEN}
    assert tokens.loads == [(TARGET, "alice", "main")]


def test_the_cancel_after_the_deadline_carries_the_callers_credentials_and_reads_no_store(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = stored_tokens()
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    result = run_connected(
        agent, TOKEN_AUTH, dependencies, lambda connection: connection.call(ALICE, "slow:5 forever", timeout_sec=0.5)
    )

    (cancel,) = sent_requests(agent, "CancelTask")
    assert result.startswith(f"⚠️ A2A_TIMEOUT ({AGENT})")
    assert cancel.headers["x-api-key"] == ALICE_TOKEN
    assert tokens.loads == [(TARGET, "alice", "main")]


def test_a_continuation_probe_carries_the_credentials_of_the_send(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = stored_tokens()
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=tokens)

    async def body(connection) -> str:
        await connection.call(ALICE, "slow:0.1 quick", timeout_sec=10.0)
        handle = dependencies.task_handles.seal(ALICE, AGENT, agent.completed[-1])
        agent.requests.clear()
        tokens.loads.clear()
        return await connection.call(ALICE, "more please", task_id=handle, timeout_sec=10.0)

    result = run_connected(agent, TOKEN_AUTH, dependencies, body)

    send, probe = agent.requests
    assert "already ended" in result
    assert (send.rpc_method, probe.rpc_method) == ("SendMessage", "GetTask")
    assert send.headers["x-api-key"] == probe.headers["x-api-key"] == ALICE_TOKEN
    assert tokens.loads == [(TARGET, "alice", "main")]


def test_status_and_cancel_tools_carry_the_calling_owners_credentials(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    contexts = recorded_contexts()
    dependencies = credential_dependencies(contexts=contexts, tokens=stored_tokens())

    async def body(connection) -> tuple[str, str]:
        short = await connection.call(ALICE, "slow:0.4 first", wait=False, timeout_sec=10.0)
        long = await connection.call(ALICE, "slow:5 second", wait=False, timeout_sec=10.0)
        agent.requests.clear()
        contexts.loads.clear()
        finished = await connection.status(ALICE, reported_task_handle(short), wait_sec=5.0, timeout_sec=10.0)
        cancelled = await connection.cancel(ALICE, reported_task_handle(long), timeout_sec=10.0)
        return finished, cancelled

    finished, cancelled = run_connected(agent, HEADERS_AUTH, dependencies, body)

    followed = sent_requests(agent, "GetTask") + sent_requests(agent, "CancelTask")
    assert "answer: slow:0.4 first" in finished
    assert "canceled" in cancelled
    assert len(sent_requests(agent, "GetTask")) >= 2
    assert len(sent_requests(agent, "CancelTask")) == 1
    assert {(request.headers["x-sudir-token"], request.headers["cookie"]) for request in followed} == {
        (ALICE_SUDIR, f"sudir={ALICE_SESSION}")
    }
    assert len(contexts.loads) == 2


def test_a_follow_up_authorizes_as_the_caller(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    contexts = FixedSecurityContextStore(
        {ALICE.security_token: context_of(headers={"x-sudir-token": "t1"}, cookies={"sudir": "c1"})}
    )
    dependencies = credential_dependencies(contexts=contexts, tokens=stored_tokens())
    later = scope_in_another_chat("alice", "main", SurfaceId.BROWSER)

    async def body(connection) -> str:
        submitted = await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0)
        contexts.contexts[ALICE.security_token] = context_of(headers={"x-sudir-token": "t2"}, cookies={"sudir": "c2"})
        return await connection.status(later, reported_task_handle(submitted), timeout_sec=10.0)

    run_connected(agent, HEADERS_AUTH, dependencies, body)

    (send,) = sent_requests(agent, "SendMessage")
    (read,) = sent_requests(agent, "GetTask")
    assert send.headers["x-sudir-token"] == "t1"
    assert (read.headers["x-sudir-token"], read.headers["cookie"]) == ("t2", "sudir=c2")


def test_concurrent_owners_each_carry_their_own_credentials(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=stored_tokens())

    async def body(connection) -> list[str]:
        return await asyncio.gather(
            connection.call(ALICE, "slow:0.3 alice", timeout_sec=10.0),
            connection.call(BOB, "slow:0.3 bob", timeout_sec=10.0),
        )

    results = run_connected(agent, TOKEN_AUTH, dependencies, body)

    polled: dict[str, set[str]] = {}
    for request in sent_requests(agent, "GetTask"):
        polled.setdefault(request.rpc_params["id"], set()).add(request.headers["x-api-key"])
    assert ["answer: slow:0.3 alice" in results[0], "answer: slow:0.3 bob" in results[1]] == [True, True]
    assert {_text(request): request.headers["x-api-key"] for request in sent_requests(agent, "SendMessage")} == {
        "slow:0.3 alice": ALICE_TOKEN,
        "slow:0.3 bob": STATIC,
    }
    assert sorted(len(values) for values in polled.values()) == [1, 1]
    assert set().union(*polled.values()) == {ALICE_TOKEN, STATIC}


@parametrize_from_dict(EVERY_KIND)
def test_a_server_set_cookie_does_not_reach_another_owner(auth, monkeypatch) -> None:
    agent = build_fake_agent(AGENT)
    agent.app = SetCookieMiddleware(agent.app, "session=s1; Path=/")
    install_fake_transport(monkeypatch, agent)
    dependencies = credential_dependencies(contexts=recorded_contexts(), tokens=stored_tokens())

    async def body(connection) -> list[str]:
        return [await connection.call(scope, "slow:0.1 hi", timeout_sec=10.0) for scope in (ALICE, BOB, ALICE)]

    results = run_connected(agent, auth, dependencies, body)

    assert all("answer: slow:0.1 hi" in result for result in results)
    assert len(sent_requests(agent, "SendMessage")) == 3
    assert all("session=s1" not in request.headers.get("cookie", "") for request in agent.requests)


def test_trace_and_agent_headers_ride_beside_credentials(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ID", "agent-under-test")
    agent = fake_crm_agent(monkeypatch)
    dependencies = credential_dependencies(contexts=unread_contexts(), tokens=stored_tokens())

    async def body(connection) -> str:
        with aef_tracing.trace_scope(CALLER):
            return await connection.call(ALICE, "hello", timeout_sec=10.0)

    run_connected(agent, TOKEN_AUTH, dependencies, body)

    (send,) = sent_requests(agent, "SendMessage")
    assert send.headers[aef_tracing.TRACE_HEADER_NAME.lower()] == CALLER
    assert send.headers[aef_tracing.AGENT_HEADER_NAME.lower()] == "agent-under-test"
    assert send.headers["a2a-version"] == "1.0"
    assert send.headers["x-api-key"] == ALICE_TOKEN


def test_a_subtask_carries_its_parents_credentials(background_loop, tmp_path, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    tokens = stored_tokens()
    manager = A2aManager()
    provider = A2aToolProvider(
        manager,
        10,
        agents=[entry_for(agent, TOKEN_AUTH)],
        loop=background_loop,
        dependencies=credential_dependencies(contexts=unread_contexts(), tokens=tokens),
        egress_enabled=True,
    )
    parent = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=ALICE)
    delivered = decode_agent_event(
        encode_agent_event(SubtaskRequested(scope=parent.scope, task_id="child", description="look it up"))
    )
    subtask = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        scope=_task_scope({"id": "child", "scope": delivered.scope.to_dict()}),
    )

    provider.refresh()
    try:
        result = asyncio.run(provider.execute(subtask, f"a2a__{AGENT}", {"message": "hello"}))
    finally:
        call_on_loop_sync(background_loop, manager.shutdown, timeout_sec=10)

    (send,) = sent_requests(agent, "SendMessage")
    assert "answer: hello" in result
    assert send.headers["x-api-key"] == ALICE_TOKEN
    assert tokens.loads == [(TARGET, "alice", "main")]


@parametrize_from_dict({"user_headers": {"auth": HEADERS_AUTH}, "user_token": {"auth": TOKEN_AUTH}})
def test_a_stored_value_reaches_only_the_configured_origin(auth, monkeypatch) -> None:
    agent = build_fake_agent(AGENT)
    interfaces = [
        AgentInterface(url="https://other.example.test/crm/", protocol_binding=TransportProtocol.JSONRPC),
        *agent.card.supported_interfaces,
    ]
    del agent.card.supported_interfaces[:]
    agent.card.supported_interfaces.extend(interfaces)
    install_fake_transport(monkeypatch, agent)
    dependencies = credential_dependencies(contexts=recorded_contexts(), tokens=stored_tokens())

    result = called(agent, auth, dependencies, ALICE, "slow:0.2 hi")

    carrying = [
        request
        for request in agent.requests
        if any(value in header for header in request.headers.values() for value in (ALICE_TOKEN, ALICE_SUDIR))
    ]
    assert "answer: slow:0.2 hi" in result
    assert len(carrying) >= 2
    assert {request.headers["host"] for request in agent.requests} == {"fake-a2a.test"}
