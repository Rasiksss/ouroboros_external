"""Outbound A2A client — discovery, the published tool, and the remote task lifecycle.

Async bodies run through ``asyncio.run()``, as in ``tests/ouroboros/mcp/test_mcp_client.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import httpx
import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a import transports
from ouroboros.egress.a2a.connection import (
    CANCEL_ON_TIMEOUT_BUDGET_SEC,
    DESCRIPTION_MAX_CHARS,
    A2aRemoteAgentConnection,
    build_a2a_tool_name,
    tool_ceiling_sec,
)
from ouroboros.egress.a2a.manager import A2aManager
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from ouroboros.tools.naming import PROVIDER_NAME_RE
from tests.helpers.egress_auth import (
    a2a_egress_dependencies,
    reported_task_handle,
    unread_egress_auth_dependencies,
)
from tests.test_helper.a2a import build_fake_agent, install_fake_transport

SCOPE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
DEPENDENCIES = a2a_egress_dependencies(unread_egress_auth_dependencies())


def _entry(agent, **overrides) -> A2aRemoteAgentEntry:
    payload = {
        "id": agent.agent_id,
        "name": "Fake",
        "url": agent.url,
        "poll_interval_sec": 0.05,
    }
    payload.update(overrides)
    return A2aRemoteAgentEntry(**payload)


async def _connected(agent, **overrides) -> A2aRemoteAgentConnection:
    connection = A2aRemoteAgentConnection(_entry(agent, **overrides), DEPENDENCIES)
    await connection.connect()
    return connection


def _raw_task_id(agent_id: str, handle: str) -> str | None:
    return DEPENDENCIES.task_handles.open(SCOPE, agent_id, handle)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_tool_name_is_provider_safe_and_one_per_agent():
    assert build_a2a_tool_name("researcher") == "a2a__researcher"
    odd = build_a2a_tool_name("my agent/2")
    assert PROVIDER_NAME_RE.match(odd), odd
    long_name = build_a2a_tool_name("x" * 200)
    assert len(long_name) == 64
    assert long_name != build_a2a_tool_name("x" * 201)


# ---------------------------------------------------------------------------
# Discovery and the published tool
# ---------------------------------------------------------------------------


def test_discovery_publishes_one_tool_describing_every_skill(monkeypatch):
    agent = build_fake_agent("researcher", skills=[("search", "Finds papers"), ("summarise", "Condenses them")])
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            assert connection.card_available is True
            assert connection.tool_name == "a2a__researcher"
            assert "search — Finds papers" in connection.schema.description
            assert "summarise — Condenses them" in connection.schema.description
            properties = connection.schema.parameters["properties"]
            assert set(properties) == {"message", "wait", "task_id"}
            assert connection.schema.parameters["required"] == ["message"]
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_flood_of_skills_cannot_exceed_the_description_cap(monkeypatch):
    agent = build_fake_agent(
        "verbose",
        skills=[(f"skill_{i}", "d" * 200) for i in range(27)],
    )
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            assert len(connection.schema.description) == DESCRIPTION_MAX_CHARS
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_featured_skills_select_what_the_description_mentions(monkeypatch):
    agent = build_fake_agent("picky", skills=[("kept", "Stays"), ("dropped", "Goes")])
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent, featured_skills=["kept"])
        try:
            assert "kept — Stays" in connection.schema.description
            assert "dropped" not in connection.schema.description
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_an_unreachable_card_degrades_to_a_bare_message_tool(monkeypatch):
    reachable = build_fake_agent("live")
    install_fake_transport(monkeypatch, reachable)

    async def scenario():
        connection = A2aRemoteAgentConnection(
            A2aRemoteAgentEntry(id="ghost", url="http://fake-a2a.test/ghost", poll_interval_sec=0.05), DEPENDENCIES
        )
        await connection.connect()
        try:
            assert connection.card_available is False
            assert set(connection.schema.parameters["properties"]) == {"message", "wait", "task_id"}
            assert "could not be read" in connection.schema.description
            assert len(connection.schema.description) <= DESCRIPTION_MAX_CHARS
            result = await connection.call(SCOPE, "anything", timeout_sec=1.0)
            assert result.startswith(("⚠️ A2A_ERROR (ghost)", "⚠️ A2A_TIMEOUT (ghost)"))
        finally:
            await connection.aclose()

    asyncio.run(scenario())


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Passes every request through, noting what the trace header carried.

    Wrapped around the real transport rather than replacing it, because what is under test is
    the header on a request the production path actually built: ``install_fake_transport``
    constructs its own client and so drops the event hook that stamps it.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, seen: list[str | None]) -> None:
        self._inner = inner
        self._seen = seen

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._seen.append(request.headers.get(aef_tracing.TRACE_HEADER_NAME))
        return await self._inner.handle_async_request(request)


def _record_discovery(monkeypatch, seen: list[str | None], inner: httpx.AsyncBaseTransport) -> None:
    real_build = transports.build_http_client

    def _build(entry, forwarded_headers):
        client = real_build(entry, forwarded_headers)
        client._transport = _RecordingTransport(inner, seen)
        return client

    monkeypatch.setattr(transports, "build_http_client", _build)


def test_card_discovery_carries_an_identifier_of_its_own(monkeypatch):
    agent = build_fake_agent("traced")
    seen: list[str | None] = []
    _record_discovery(monkeypatch, seen, httpx.ASGITransport(app=agent.app))

    async def scenario():
        connection = A2aRemoteAgentConnection(_entry(agent), DEPENDENCIES)
        await connection.connect()
        try:
            assert connection.card_available is True
        finally:
            await connection.aclose()

    ambient = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    with aef_tracing.trace_scope(ambient):
        asyncio.run(scenario())
        assert aef_tracing.current_trace_id() == ambient

    assert len(seen) == 1, seen
    minted = seen[0]
    assert minted and aef_tracing.normalize_trace_id(minted) == minted
    assert minted != ambient


def test_an_unreachable_card_is_traced_too(monkeypatch):
    seen: list[str | None] = []
    _record_discovery(monkeypatch, seen, httpx.MockTransport(lambda request: httpx.Response(503)))

    async def scenario():
        connection = A2aRemoteAgentConnection(
            A2aRemoteAgentEntry(id="ghost", url="http://fake-a2a.test/ghost", poll_interval_sec=0.05), DEPENDENCIES
        )
        await connection.connect()
        try:
            assert connection.card_available is False
        finally:
            await connection.aclose()

    asyncio.run(scenario())

    assert seen and all(value and aef_tracing.normalize_trace_id(value) == value for value in seen), seen
    assert aef_tracing.current_trace_id() == ""


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


def test_a_waiting_call_returns_the_finished_artifact(monkeypatch):
    agent = build_fake_agent("worker")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "slow:0.2 crunch numbers", timeout_sec=10.0)
            assert "answer: slow:0.2 crunch numbers" in result
            assert not result.startswith("⚠️")
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_non_blocking_call_returns_a_task_id_before_the_work_is_done(monkeypatch):
    agent = build_fake_agent("worker")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            started = time.monotonic()
            result = await connection.call(SCOPE, "slow:2 long job", wait=False, timeout_sec=10.0)
            elapsed = time.monotonic() - started
            assert elapsed < 1.0, f"submit took {elapsed:.2f}s"
            assert "task_id=" in result
            assert "a2a_task_status" in result
            assert not result.startswith("⚠️")

            task_id = result.split("task_id=", 1)[1].split()[0]
            assert task_id == reported_task_handle(result)
            assert f"A2A task {task_id} on worker" in await connection.status(SCOPE, task_id, timeout_sec=10.0)
            finished = await connection.status(SCOPE, task_id, wait_sec=10.0, timeout_sec=10.0)
            assert "answer: slow:2 long job" in finished
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_timeout_cancels_the_remote_task_and_keeps_its_id(monkeypatch):
    agent = build_fake_agent("slowpoke")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "slow:5 forever", timeout_sec=0.3)
            assert result.startswith("⚠️ A2A_TIMEOUT (slowpoke)")
            assert "task_id=" in result
            assert "cancel requested" in result

            task_id = result.split("task_id=", 1)[1].strip()
            state = await connection.status(SCOPE, task_id, timeout_sec=10.0)
            assert "canceled" in state
            assert _raw_task_id("slowpoke", task_id) not in agent.completed
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_send_that_is_never_answered_is_unknown_rather_than_refused(monkeypatch):
    agent = build_fake_agent("mute-accept")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        original = connection._send

        async def _never_answers(context, text, task_id):
            await asyncio.sleep(30)
            return await original(context, text, task_id)

        connection._send = _never_answers
        try:
            result = await connection.call(SCOPE, "start the nightly report", timeout_sec=0.3)
            assert result.startswith("⚠️ A2A_SUBMISSION_UNKNOWN (mute-accept)")
            assert "Do not resend" in result
            assert "did not accept" not in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_an_unanswered_continuation_points_at_the_task_it_was_sent_against(monkeypatch):
    agent = build_fake_agent("curious-mute")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            asked = await connection.call(SCOPE, "ask about the repo", timeout_sec=10.0)
            task_id = asked.split("task_id=", 1)[1].split()[0]

            original = connection._send

            async def _never_answers(context, text, requested):
                await asyncio.sleep(30)
                return await original(context, text, requested)

            connection._send = _never_answers
            result = await connection.call(SCOPE, "the ouroboros one", task_id=task_id, timeout_sec=0.3)
            assert result.startswith("⚠️ A2A_SUBMISSION_UNKNOWN (curious-mute)")
            assert task_id in result
            assert "a2a_task_status" in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_read_timeout_on_the_send_is_unknown_too(monkeypatch):
    agent = build_fake_agent("laggy-read")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)

        async def _read_timeout(context, text, task_id):
            try:
                raise httpx.ReadTimeout("timed out waiting for the response")
            except httpx.ReadTimeout as exc:
                raise RuntimeError("A2A client error: the request failed") from exc

        connection._send = _read_timeout
        try:
            result = await connection.call(SCOPE, "anything", timeout_sec=10.0)
            assert result.startswith("⚠️ A2A_SUBMISSION_UNKNOWN (laggy-read)")
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_connection_that_never_opened_is_a_plain_error(monkeypatch):
    agent = build_fake_agent("refused")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)

        async def _refused(context, text, task_id):
            raise httpx.ConnectError("connection refused")

        connection._send = _refused
        try:
            result = await connection.call(SCOPE, "anything", timeout_sec=10.0)
            assert result.startswith("⚠️ A2A_ERROR (refused)")
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_question_comes_back_as_a_result_and_can_be_answered(monkeypatch):
    agent = build_fake_agent("curious")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            asked = await connection.call(SCOPE, "ask about the repo", timeout_sec=10.0)
            assert asked.startswith("⚠️ A2A_INPUT_REQUIRED (curious)")
            assert "Which repository should I look at?" in asked
            assert "task_id=" in asked

            task_id = asked.split("task_id=", 1)[1].split()[0]
            assert "canceled" not in await connection.status(SCOPE, task_id, timeout_sec=10.0)

            answered = await connection.call(SCOPE, "the ouroboros one", task_id=task_id, timeout_sec=10.0)
            assert "answer: the ouroboros one" in answered
            assert agent.completed == [_raw_task_id("curious", task_id)]
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_answering_a_finished_task_is_explained_not_raised(monkeypatch):
    agent = build_fake_agent("done")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            first = await connection.call(SCOPE, "slow:0.1 quick", timeout_sec=10.0)
            assert not first.startswith("⚠️")
            task_id = DEPENDENCIES.task_handles.seal(SCOPE, "done", agent.completed[-1])

            result = await connection.call(SCOPE, "more please", task_id=task_id, timeout_sec=10.0)
            assert result.startswith("⚠️ A2A_ERROR (done)")
            assert "already ended" in result
            assert "completed" in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_failed_remote_task_is_a_tool_failure(monkeypatch):
    agent = build_fake_agent("broken")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "boom", timeout_sec=10.0)
            assert result.startswith("⚠️ A2A_ERROR (broken)")
            assert "failed" in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_rejected_remote_task_is_a_tool_failure(monkeypatch):
    agent = build_fake_agent("picky")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "reject this", timeout_sec=10.0)
            assert result.startswith("⚠️ A2A_ERROR (picky)")
            assert "rejected" in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_a_task_that_finishes_empty_says_so_rather_than_returning_nothing(monkeypatch):
    agent = build_fake_agent("mute")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "quiet please", timeout_sec=10.0)
            assert "completed with no output" in result
            assert not result.startswith("⚠️")
        finally:
            await connection.aclose()

    asyncio.run(scenario())


def test_an_agent_that_answers_without_a_task_is_handled(monkeypatch):
    agent = build_fake_agent("chatty")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            result = await connection.call(SCOPE, "message-only hello", timeout_sec=10.0)
            assert "direct reply: message-only hello" in result
            assert "task_id" not in result
        finally:
            await connection.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def test_a_dead_agent_does_not_hide_the_live_ones(monkeypatch):
    live_a = build_fake_agent("alpha")
    live_b = build_fake_agent("beta")
    install_fake_transport(monkeypatch, live_a, live_b)

    async def scenario():
        manager = A2aManager()
        await manager.initialize(
            [
                _entry(live_a),
                A2aRemoteAgentEntry(id="ghost", url="http://fake-a2a.test/ghost"),
                _entry(live_b),
            ],
            asyncio.get_running_loop(),
            True,
            DEPENDENCIES,
        )
        try:
            names = {connection.tool_name for connection in manager.connections()}
            assert {"a2a__alpha", "a2a__beta"} <= names
            assert manager.connection_for("alpha").card_available is True
            assert manager.connection_for("ghost").card_available is False
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_a_name_collision_keeps_the_first_agent(monkeypatch):
    first = build_fake_agent("dup")
    install_fake_transport(monkeypatch, first)

    async def scenario():
        manager = A2aManager()
        await manager.initialize(
            [
                A2aRemoteAgentEntry(id="dup", url=first.url),
                A2aRemoteAgentEntry(id="dup", url=first.url),
            ],
            asyncio.get_running_loop(),
            True,
            DEPENDENCIES,
        )
        try:
            assert len(manager.connections()) == 1
            assert manager.resolve("a2a__dup") == "dup"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_only_the_first_entry_of_an_id_connects(monkeypatch, caplog):
    agent = build_fake_agent("crm")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize(
            [
                A2aRemoteAgentEntry(id="hr", url=agent.url, enabled=False),
                A2aRemoteAgentEntry(id="crm", name="First", url=agent.url),
                A2aRemoteAgentEntry(id=" crm ", name="Second", url=agent.url),
            ],
            asyncio.get_running_loop(),
            True,
            DEPENDENCIES,
        )
        try:
            return [(connection.entry.id, connection.entry.name) for connection in manager.connections()]
        finally:
            await manager.shutdown()

    with caplog.at_level(logging.WARNING, logger="ouroboros.egress.a2a.manager"):
        connected = asyncio.run(scenario())

    assert connected == [("crm", "First")]
    assert [request.method for request in agent.requests] == ["GET"]
    assert [record.getMessage() for record in caplog.records] == [
        "Duplicate A2A agent id crm in settings; keeping the first entry"
    ]


def test_a_call_from_another_loop_reaches_the_one_the_client_lives_on(monkeypatch):
    agent = build_fake_agent("bridged")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        client_loop = asyncio.get_running_loop()
        await manager.initialize([_entry(agent)], client_loop, True, DEPENDENCIES)
        try:

            def _other_loop() -> str:
                return asyncio.run(manager.call_tool(SCOPE, "bridged", {"message": "slow:0.2 hi"}, 10.0))

            text = await asyncio.to_thread(_other_loop)
            assert threading.current_thread() is threading.main_thread()
            assert "answer: slow:0.2 hi" in text
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_an_unknown_agent_id_fails_cleanly(monkeypatch):
    agent = build_fake_agent("known")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        try:
            result = await manager.call_tool(SCOPE, "nobody", {"message": "hi"}, 1.0)
            assert result.startswith("⚠️ A2A_ERROR (nobody)")
            assert "unknown remote agent" in result
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("egress_enabled", [True, False])
def test_a_disabled_egress_connects_to_nobody(monkeypatch, egress_enabled):
    agent = build_fake_agent("switch")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        assert manager.is_available() is False
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), egress_enabled, DEPENDENCIES)
        try:
            assert manager.is_available() is True
            assert bool(manager.connections()) is egress_enabled
        finally:
            await manager.shutdown()
        assert manager.is_available() is False

    asyncio.run(scenario())


def test_shutdown_is_idempotent(monkeypatch):
    agent = build_fake_agent("closer")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.shutdown()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        await manager.shutdown()
        await manager.shutdown()
        assert manager.connections() == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Deadlines, order, and lifecycle
# ---------------------------------------------------------------------------


def test_a_status_read_gets_the_tool_timeout_not_its_own_wait_budget(monkeypatch):
    agent = build_fake_agent("poller")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        try:
            submitted = await manager.call_tool(SCOPE, "poller", {"message": "slow:3 long", "wait": False}, 30.0)
            task_id = submitted.split("task_id=", 1)[1].split()[0]

            connection = manager.connection_for("poller")
            original = connection._get_task

            async def _slow_get(context, requested):
                await asyncio.sleep(2.5)
                return await original(context, requested)

            connection._get_task = _slow_get
            result = await manager.task_status(SCOPE, "poller", task_id, 0.0, 30.0)
            assert not result.startswith("⚠️"), result
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_a_slow_send_spends_the_call_deadline_rather_than_the_bridge_grace(monkeypatch):
    agent = build_fake_agent("laggy")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        connection = manager.connection_for("laggy")
        original = connection._send

        async def _slow_send(context, text, task_id):
            await asyncio.sleep(2.5)
            return await original(context, text, task_id)

        connection._send = _slow_send
        try:
            result = await manager.call_tool(SCOPE, "laggy", {"message": "slow:10 forever"}, 5.0)
            assert result.startswith("⚠️ A2A_TIMEOUT (laggy)")
            assert "task_id=" in result
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_a_cancel_slower_than_the_bridge_grace_still_reports_the_task_id(monkeypatch):
    agent = build_fake_agent("stubborn")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        connection = manager.connection_for("stubborn")
        original = connection._cancel_task

        async def _slow_cancel(context, task_id):
            await asyncio.sleep(3.0)
            return await original(context, task_id)

        connection._cancel_task = _slow_cancel
        try:
            started = time.monotonic()
            result = await manager.call_tool(SCOPE, "stubborn", {"message": "slow:5 forever"}, 0.3)
            elapsed = time.monotonic() - started

            assert result.startswith("⚠️ A2A_TIMEOUT (stubborn)")
            assert "task_id=" in result
            assert "cancel requested" in result
            assert 3.0 < elapsed < 0.3 + CANCEL_ON_TIMEOUT_BUDGET_SEC
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_the_tool_ceiling_leaves_room_for_the_cancel_and_the_hop():
    """What the agent loop must allow, against what the call itself gets."""
    assert tool_ceiling_sec(300) > 300 + CANCEL_ON_TIMEOUT_BUDGET_SEC
    assert isinstance(tool_ceiling_sec(0.5), int)


def test_wait_null_is_the_documented_default_rather_than_its_opposite(monkeypatch):
    agent = build_fake_agent("defaults")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        try:
            for arguments in (
                {"message": "hello"},
                {"message": "hello", "wait": None},
                {"message": "hello", "wait": ""},
            ):
                result = await manager.call_tool(SCOPE, "defaults", arguments, 10.0)
                assert "answer: hello" in result, arguments
            explicit = await manager.call_tool(SCOPE, "defaults", {"message": "hello", "wait": False}, 10.0)
            assert "task_id=" in explicit
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_an_id_written_with_a_stray_space_is_still_dispatchable(monkeypatch):
    agent = build_fake_agent("spaced")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        await manager.initialize(
            [A2aRemoteAgentEntry(id=" spaced ", url=f" {agent.url} ", poll_interval_sec=0.05)],
            asyncio.get_running_loop(),
            True,
            DEPENDENCIES,
        )
        try:
            assert [connection.tool_name for connection in manager.connections()] == ["a2a__spaced"]
            agent_id = manager.resolve("a2a__spaced")
            assert agent_id == "spaced"
            assert "answer: hi" in await manager.call_tool(SCOPE, agent_id, {"message": "hi"}, 10.0)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_the_tool_order_follows_configuration_not_who_answered_first(monkeypatch):
    first = build_fake_agent("aaa")
    second = build_fake_agent("bbb")
    install_fake_transport(monkeypatch, first, second)

    async def scenario():
        manager = A2aManager()
        entries = [_entry(first), _entry(second)]
        original = A2aRemoteAgentConnection.connect

        async def _staggered(self):
            await asyncio.sleep(0.2 if self.entry.id == "aaa" else 0.0)
            await original(self)

        monkeypatch.setattr(A2aRemoteAgentConnection, "connect", _staggered)
        await manager.initialize(entries, asyncio.get_running_loop(), True, DEPENDENCIES)
        try:
            assert [connection.entry.id for connection in manager.connections()] == ["aaa", "bbb"]
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_a_connect_that_fails_late_leaves_no_open_http_client(monkeypatch):
    agent = build_fake_agent("leaky")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        clients = []
        original = transports.build_http_client

        def _tracking(entry, forwarded_headers):
            client = original(entry, forwarded_headers)
            clients.append(client)
            return client

        monkeypatch.setattr(transports, "build_http_client", _tracking)
        monkeypatch.setattr(
            transports,
            "build_a2a_client",
            lambda card, http_client: (_ for _ in ()).throw(ValueError("no compatible transports found.")),
        )

        manager = A2aManager()
        await manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES)
        try:
            assert manager.connections() == []
            assert clients and all(client.is_closed for client in clients)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_shutdown_closes_what_an_interrupted_initialize_opened(monkeypatch):
    agent = build_fake_agent("halfway")
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        manager = A2aManager()
        original = A2aRemoteAgentConnection.connect
        opened: list[A2aRemoteAgentConnection] = []

        async def _connect_then_hang(self):
            await original(self)
            opened.append(self)
            await asyncio.sleep(60)

        monkeypatch.setattr(A2aRemoteAgentConnection, "connect", _connect_then_hang)
        task = asyncio.create_task(manager.initialize([_entry(agent)], asyncio.get_running_loop(), True, DEPENDENCIES))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert opened, "the connection was never built"
        assert manager.is_available() is False
        await manager.shutdown()
        assert all(connection._http_client is None for connection in opened)

    asyncio.run(scenario())


def test_interrupted_initialize_closes_a_peer_that_finished_before_its_sibling(monkeypatch):
    fast = build_fake_agent("fast")
    slow = build_fake_agent("slow")
    install_fake_transport(monkeypatch, fast, slow)

    async def scenario():
        manager = A2aManager()
        original = A2aRemoteAgentConnection.connect
        opened: list[A2aRemoteAgentConnection] = []
        slow_opened = asyncio.Event()

        async def _connect_then_stall_one(self):
            await original(self)
            opened.append(self)
            if self.entry.id == "slow":
                slow_opened.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(A2aRemoteAgentConnection, "connect", _connect_then_stall_one)
        task = asyncio.create_task(
            manager.initialize([_entry(fast), _entry(slow)], asyncio.get_running_loop(), True, DEPENDENCIES)
        )
        await slow_opened.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert {connection.entry.id for connection in opened} == {"fast", "slow"}
        assert manager.connections() == []
        assert manager.is_available() is False
        assert all(connection._http_client is None for connection in opened)

    asyncio.run(scenario())


def test_published_skills_survive_the_hundred_and_twenty_character_preview(monkeypatch):
    """``list_available_tools`` shows ``description[:120]``, and that line is where the model
    picks an agent."""
    agent = build_fake_agent(
        "researcher",
        skills=[("literature_search", "Finds papers on a topic"), ("citation_check", "Verifies citations")],
    )
    install_fake_transport(monkeypatch, agent)

    async def scenario():
        connection = await _connected(agent)
        try:
            assert "literature_search" in connection.schema.description[:120]
        finally:
            await connection.aclose()

    asyncio.run(scenario())
