from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from tests.helpers.a2a_credentials import AGENT, TOKEN_AUTH, fake_crm_agent, run_connected, stored_tokens
from tests.helpers.egress_auth import A2A_TASK_HANDLE_RE, a2a_egress_dependencies, reported_task_handle
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import FixedSecurityContextStore
from tests.test_helper.a2a.scripted_agent import ScriptedAgent

ALICE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="c1")
BOB = OwnerScope(owner_id="bob", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="c1")
REMOTE_TASK = "remote-task-1"


class _Stores:
    def __init__(self) -> None:
        self.contexts = FixedSecurityContextStore({})
        self.tokens = stored_tokens()
        self.dependencies: A2aEgressDependencies = a2a_egress_dependencies(
            EgressAuthDependencies(security_context_store=self.contexts, user_token_store=self.tokens)
        )

    def reads(self) -> int:
        return len(self.contexts.loads) + len(self.tokens.loads)


def _run(agent: ScriptedAgent, stores: _Stores, body):
    return run_connected(agent, TOKEN_AUTH, stores.dependencies, body)


def _calls(agent: ScriptedAgent) -> list[str]:
    return [request.rpc_method for request in agent.requests if request.rpc_method]


def _assert_refused(result: str, value: str) -> None:
    assert result.startswith(f"⚠️ A2A_ERROR ({AGENT}):")
    assert repr(value) in result
    assert "was not started by this owner and agent" in result
    assert "exactly as" in result


_FOLLOW_UPS = {
    "status": {"follow": lambda connection, scope, handle: connection.status(scope, handle, timeout_sec=10.0)},
    "cancel": {"follow": lambda connection, scope, handle: connection.cancel(scope, handle, timeout_sec=10.0)},
    "continuation": {
        "follow": lambda connection, scope, handle: connection.call(scope, "answer", task_id=handle, timeout_sec=10.0)
    },
}


@parametrize_from_dict(_FOLLOW_UPS)
def test_another_owners_handle_is_refused_before_any_store_read_or_request(follow, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()
    handle = stores.dependencies.task_handles.seal(ALICE, AGENT, REMOTE_TASK)

    result = _run(agent, stores, lambda connection: follow(connection, BOB, handle))

    _assert_refused(result, handle)
    assert stores.reads() == 0
    assert _calls(agent) == []


@parametrize_from_dict(
    {
        "another agent": {
            "sealed_for": OwnerScope(owner_id="alice", agent_id="helper", surface_id=SurfaceId.BROWSER, chat_id="c1"),
            "remote_agent": AGENT,
        },
        "another remote agent": {"sealed_for": ALICE, "remote_agent": "hr"},
    }
)
def test_a_handle_of_another_agent_or_remote_agent_is_refused(sealed_for, remote_agent, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()
    handle = stores.dependencies.task_handles.seal(sealed_for, remote_agent, REMOTE_TASK)

    result = _run(agent, stores, lambda connection: connection.status(ALICE, handle, timeout_sec=10.0))

    _assert_refused(result, handle)
    assert stores.reads() == 0
    assert _calls(agent) == []


def _forged(handle: str) -> str:
    return handle[:-1] + ("0" if handle[-1] != "0" else "1")


@parametrize_from_dict(
    {
        "bare": {"forge": lambda handle: handle.rpartition("~")[0], "message": "is not a sealed handle"},
        "forged": {"forge": _forged, "message": "was not started by this owner and agent"},
    }
)
def test_a_bare_or_forged_task_id_is_refused_without_a_request(forge, message, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()

    async def body(connection) -> tuple[str, str]:
        submitted = await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0)
        value = forge(reported_task_handle(submitted))
        agent.requests.clear()
        stores.tokens.loads.clear()
        return value, await connection.status(ALICE, value, timeout_sec=10.0)

    value, result = _run(agent, stores, body)

    assert result.startswith(f"⚠️ A2A_ERROR ({AGENT}):")
    assert repr(value) in result
    assert message in result
    assert "exactly as" in result
    assert stores.reads() == 0
    assert _calls(agent) == []


def test_a_handle_opens_from_another_chat_and_surface_of_the_same_owner_and_agent(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()
    dialogs = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.DIALOGS, chat_id="c2")

    async def body(connection) -> tuple[str, str]:
        handle = reported_task_handle(await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0))
        return handle, await connection.status(dialogs, handle, timeout_sec=10.0)

    handle, result = _run(agent, stores, body)

    (read,) = [request for request in agent.requests if request.rpc_method == "GetTask"]
    assert not result.startswith("⚠️")
    assert read.rpc_params["id"] == stores.dependencies.task_handles.open(dialogs, AGENT, handle)


def test_the_remote_agent_never_receives_a_handle(monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()

    async def body(connection) -> tuple[str, str]:
        asked = reported_task_handle(await connection.call(ALICE, "ask about the repo", timeout_sec=10.0))
        answered = await connection.call(ALICE, "the ouroboros one", task_id=asked, timeout_sec=10.0)
        assert "answer: the ouroboros one" in answered
        submitted = reported_task_handle(await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0))
        await connection.status(ALICE, submitted, timeout_sec=10.0)
        await connection.cancel(ALICE, submitted, timeout_sec=10.0)
        return asked, submitted

    asked, submitted = _run(agent, stores, body)

    handles = stores.dependencies.task_handles
    raw_ids = {handles.open(ALICE, AGENT, asked), handles.open(ALICE, AGENT, submitted)}
    continuations = [
        request.rpc_params["message"]["taskId"]
        for request in agent.requests
        if request.rpc_method == "SendMessage" and "taskId" in request.rpc_params["message"]
    ]
    followed = {
        request.rpc_params["id"] for request in agent.requests if request.rpc_method in ("GetTask", "CancelTask")
    }
    assert continuations == [handles.open(ALICE, AGENT, asked)]
    assert followed == raw_ids
    for request in agent.requests:
        wire = json.dumps(request.rpc_params) + json.dumps(dict(request.headers))
        for handle in (asked, submitted):
            assert handle.rpartition("~")[2] not in wire


async def _unanswered_continuation(connection) -> str:
    asked = reported_task_handle(await connection.call(ALICE, "ask about the repo", timeout_sec=10.0))

    async def never_answers(context, text, task_id):
        await asyncio.sleep(30)
        return []

    connection._send = never_answers
    return await connection.call(ALICE, "the ouroboros one", task_id=asked, timeout_sec=0.3)


async def _submitted_then(connection, follow) -> str:
    handle = reported_task_handle(await connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0))
    return await follow(connection, handle)


_NAMING_A_TASK = {
    "submitted": lambda connection: connection.call(ALICE, "slow:5 long", wait=False, timeout_sec=10.0),
    "running": lambda connection: _submitted_then(
        connection, lambda connection, handle: connection.status(ALICE, handle, timeout_sec=10.0)
    ),
    "completed without output": lambda connection: connection.call(ALICE, "quiet please", timeout_sec=10.0),
    "failed": lambda connection: connection.call(ALICE, "boom", timeout_sec=10.0),
    "input-required": lambda connection: connection.call(ALICE, "ask about the repo", timeout_sec=10.0),
    "auth-required": lambda connection: connection.call(ALICE, "auth please", timeout_sec=10.0),
    "timed out": lambda connection: connection.call(ALICE, "slow:5 forever", timeout_sec=0.3),
    "cancelled": lambda connection: _submitted_then(
        connection, lambda connection, handle: connection.cancel(ALICE, handle, timeout_sec=10.0)
    ),
    "unanswered continuation": _unanswered_continuation,
}


@parametrize_from_dict(
    {
        **{name: {"body": body, "names_a_task": True} for name, body in _NAMING_A_TASK.items()},
        "a message answer": {
            "body": lambda connection: connection.call(ALICE, "message-only hello", timeout_sec=10.0),
            "names_a_task": False,
        },
    }
)
def test_every_result_naming_a_task_names_its_handle(body, names_a_task, monkeypatch) -> None:
    agent = fake_crm_agent(monkeypatch)
    stores = _Stores()

    result = _run(agent, stores, body)

    handles = A2A_TASK_HANDLE_RE.findall(result)
    assert bool(handles) is names_a_task, result
    for handle in handles:
        raw = stores.dependencies.task_handles.open(ALICE, AGENT, handle)
        assert raw is not None, handle
        assert raw not in result.replace(handle, "")
