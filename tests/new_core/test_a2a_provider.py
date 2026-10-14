"""The A2A tool provider — what it offers, what it dispatches, and what the gates do to it."""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import threading
from types import SimpleNamespace

import pytest

from new_core.identity import OwnerScope, SurfaceId
from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.tools.providers.a2a import (
    TASK_CANCEL_TOOL,
    TASK_STATUS_TOOL,
    A2aToolProvider,
)
from new_core.tools.schema import ToolSchema
from ouroboros.agent.loop_adapters import build_tool_call_executor
from ouroboros.agent.loop_tool_execution import ToolCallExecutor
from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
from ouroboros.policy import safety
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import a2a_egress_dependencies, null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import TEST_SCOPE, collecting_event_sink, make_catalog, make_tool_ctx, run_tool

DEPENDENCIES = a2a_egress_dependencies(null_egress_auth_dependencies())


@pytest.fixture
def background_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


class _FakeConnection:
    def __init__(self, agent_id: str, *, aliases=(), result_limit: int = 0) -> None:
        self.entry = SimpleNamespace(id=agent_id, retrieval_aliases=list(aliases), tool_result_limit=result_limit)
        self.tool_name = f"a2a__{agent_id}"
        self.schema = ToolSchema(
            name=self.tool_name,
            description=f"[A2A {agent_id}] Delegate to it.",
            parameters={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
        )


class _FakeManager:
    def __init__(self, *connections: _FakeConnection, available: bool = True) -> None:
        self._connections = list(connections)
        self.available = available
        self.calls: list[tuple] = []
        self.scopes: list[OwnerScope] = []
        self.initializations: list[tuple] = []

    async def initialize(self, agents, loop, egress_enabled, dependencies):
        self.initializations.append((list(agents), loop, egress_enabled, dependencies))
        self.available = True

    def is_available(self) -> bool:
        return self.available

    def connections(self) -> list[_FakeConnection]:
        return list(self._connections)

    def connection_for_tool(self, tool_name: str) -> _FakeConnection | None:
        for connection in self._connections:
            if connection.tool_name == tool_name:
                return connection
        return None

    def resolve(self, tool_name: str) -> str | None:
        connection = self.connection_for_tool(tool_name)
        return connection.entry.id if connection is not None else None

    async def call_tool(self, scope, agent_id, arguments, timeout_sec):
        self.scopes.append(scope)
        self.calls.append(("call", agent_id, dict(arguments), timeout_sec))
        return f"called {agent_id}"

    async def task_status(self, scope, agent_id, task_id, wait_sec, timeout_sec):
        self.scopes.append(scope)
        self.calls.append(("status", agent_id, task_id, wait_sec, timeout_sec))
        return f"status of {task_id} on {agent_id}"

    async def task_cancel(self, scope, agent_id, task_id, timeout_sec):
        self.scopes.append(scope)
        self.calls.append(("cancel", agent_id, task_id, timeout_sec))
        return f"cancelled {task_id} on {agent_id}"


def _provider(*connections, available=True, timeout=30):
    manager = _FakeManager(*connections, available=available)
    return A2aToolProvider(manager, timeout, agents=None, loop=None, dependencies=DEPENDENCIES), manager


def _execute(provider, name, args):
    ctx = make_tool_ctx(repo_dir=pathlib.Path(), drive_root=pathlib.Path())
    result = provider.execute(ctx, name, args)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _run_through_the_loop(tmp_path, monkeypatch, manager, arguments):
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    catalog = make_catalog([A2aToolProvider(manager, 5, agents=None, loop=None, dependencies=DEPENDENCIES)])
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return executor.execute(
        [ToolCall(id="c1", function=ToolCallFunction(name="a2a__alpha", arguments=json.dumps(arguments)))],
        ctx=ctx,
        task_id="t-1",
        active_tool_names=None,
        decisions={},
    )


# ---------------------------------------------------------------------------
# What the provider offers
# ---------------------------------------------------------------------------


def test_n_agents_cost_n_plus_two_names():
    provider, _ = _provider(_FakeConnection("alpha"), _FakeConnection("beta"), _FakeConnection("gamma"))
    names = [descriptor.name for descriptor in provider.descriptors(TEST_SCOPE)]
    assert len(names) == 5
    assert names[:3] == ["a2a__alpha", "a2a__beta", "a2a__gamma"]
    assert set(names[3:]) == {TASK_STATUS_TOOL, TASK_CANCEL_TOOL}


def test_every_owner_is_offered_the_same_agents():
    provider, _ = _provider(_FakeConnection("alpha"), _FakeConnection("beta"))
    other = OwnerScope(owner_id="someone-else", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="9")
    assert [descriptor.name for descriptor in provider.descriptors(other)] == [
        descriptor.name for descriptor in provider.descriptors(TEST_SCOPE)
    ]
    assert provider.get("a2a__alpha", other) is not None


def test_disabled_means_invisible_not_broken():
    provider, _ = _provider(_FakeConnection("alpha"), available=False)
    assert list(provider.descriptors(TEST_SCOPE)) == []
    assert provider.get("a2a__alpha", TEST_SCOPE) is None
    assert provider.get(TASK_STATUS_TOOL, TEST_SCOPE) is None


def test_no_configured_agent_offers_nothing_at_all():
    provider, _ = _provider()
    assert list(provider.descriptors(TEST_SCOPE)) == []
    # The companions serve tasks that only an agent tool can create.
    assert provider.get(TASK_STATUS_TOOL, TEST_SCOPE) is None


def test_refresh_connects_its_configured_source_once(background_loop):
    manager = _FakeManager(available=False)
    agents = [SimpleNamespace(id="alpha")]
    provider = A2aToolProvider(
        manager,
        30,
        agents=agents,
        loop=background_loop,
        dependencies=DEPENDENCIES,
        egress_enabled=True,
    )

    provider.refresh()
    provider.refresh()

    assert manager.initializations == [(agents, background_loop, True, DEPENDENCIES)]


@pytest.mark.parametrize(
    ("agents", "loop"),
    [([SimpleNamespace(id="alpha")], None), (None, "loop")],
)
def test_partial_lifecycle_ownership_is_refused(agents, loop):
    with pytest.raises(ValueError, match="agents and loop"):
        A2aToolProvider(_FakeManager(), 30, agents=agents, loop=loop, dependencies=DEPENDENCIES)


def test_successful_owned_refresh_logs_the_connection_count(background_loop, caplog):
    manager = _FakeManager(_FakeConnection("alpha"), available=False)
    provider = A2aToolProvider(
        manager,
        30,
        agents=[SimpleNamespace(id="alpha")],
        loop=background_loop,
        dependencies=DEPENDENCIES,
        egress_enabled=True,
    )

    with caplog.at_level("INFO", logger="new_core.tools.providers.a2a"):
        provider.refresh()

    assert any("initialized agents=1" in record.getMessage() for record in caplog.records)


def test_slow_discovery_uses_its_own_startup_budget(background_loop, monkeypatch):
    from new_core.tools.providers import a2a as provider_module

    class _SlowManager(_FakeManager):
        async def initialize(self, agents, loop, egress_enabled, dependencies):
            await asyncio.sleep(0.02)
            await super().initialize(agents, loop, egress_enabled, dependencies)

    monkeypatch.setattr(provider_module, "tool_ceiling_sec", lambda _timeout: 0.001)
    manager = _SlowManager(available=False)
    agents = [SimpleNamespace(id="alpha")]
    provider = A2aToolProvider(
        manager,
        1,
        agents=agents,
        loop=background_loop,
        dependencies=DEPENDENCIES,
        egress_enabled=True,
    )

    provider.refresh()

    assert manager.initializations == [(agents, background_loop, True, DEPENDENCIES)]


def test_refresh_leaves_the_single_process_lifespan_in_charge_without_a_loop():
    manager = _FakeManager(available=False)
    provider = A2aToolProvider(
        manager,
        30,
        agents=None,
        loop=None,
        dependencies=DEPENDENCIES,
        egress_enabled=True,
    )

    provider.refresh()

    assert manager.initializations == []


def test_descriptors_carry_the_agent_origin_and_its_configured_metadata():
    provider, _ = _provider(_FakeConnection("alpha", aliases=("research", "papers"), result_limit=1234))
    agent_descriptor = provider.get("a2a__alpha", TEST_SCOPE)
    assert agent_descriptor.origin == "a2a:alpha"
    assert agent_descriptor.retrieval_aliases == ("research", "papers")
    assert agent_descriptor.result_limit == 1234
    # Not the ``read_only`` fallback a name outside TOOL_SPECS would otherwise inherit.
    assert agent_descriptor.capability_class == "external_network"

    # Process-wide, so no single agent — but still `a2a:`, for the origin gates.
    assert provider.get(TASK_CANCEL_TOOL, TEST_SCOPE).origin == "a2a:*"


def test_refuse_dispatch_says_nothing():
    provider, _ = _provider(_FakeConnection("alpha"))
    assert provider.refuse_dispatch("a2a__alpha", TEST_SCOPE) is None
    assert provider.refuse_dispatch("a2a__nobody", TEST_SCOPE) is None


def test_the_agent_loop_waits_past_the_call_deadline_not_up_to_it():
    """The deadlines nest: the call's own is the innermost, and the loop's ceiling is the
    outermost. Abandoning the call at 90s would throw away the timeout result carrying the
    task id, in the seconds it spends cancelling the remote task and crossing back."""
    from ouroboros.egress.a2a.connection import BRIDGE_MARGIN_SEC

    provider, manager = _provider(_FakeConnection("alpha"), timeout=90)

    ceiling = provider.get("a2a__alpha", TEST_SCOPE).timeout_sec
    assert ceiling > 90 + BRIDGE_MARGIN_SEC
    assert provider.get(TASK_STATUS_TOOL, TEST_SCOPE).timeout_sec == ceiling

    # What the manager gets is the operation's deadline, not the ceiling.
    _execute(provider, "a2a__alpha", {"message": "hi"})
    assert manager.calls[-1] == ("call", "alpha", {"message": "hi"}, 90)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_execute_routes_each_of_the_three_tools():
    provider, manager = _provider(_FakeConnection("alpha"), timeout=45)

    assert _execute(provider, "a2a__alpha", {"message": "hi", "task_id": "t-1"}) == "called alpha"
    assert manager.calls[-1] == ("call", "alpha", {"message": "hi", "task_id": "t-1"}, 45)

    assert _execute(provider, TASK_STATUS_TOOL, {"agent_id": "alpha", "task_id": "t-1", "wait_sec": 5}) == (
        "status of t-1 on alpha"
    )
    assert manager.calls[-1] == ("status", "alpha", "t-1", 5.0, 45)

    assert _execute(provider, TASK_CANCEL_TOOL, {"agent_id": "alpha", "task_id": "t-1"}) == ("cancelled t-1 on alpha")
    assert manager.calls[-1] == ("cancel", "alpha", "t-1", 45)


def test_execute_passes_the_run_scope_to_the_manager(tmp_path):
    provider, manager = _provider(_FakeConnection("alpha"))
    scope = OwnerScope(owner_id="alice", agent_id="helper", surface_id=SurfaceId.DIALOGS, chat_id="7")
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=scope)

    for name, args in (
        ("a2a__alpha", {"message": "hi"}),
        (TASK_STATUS_TOOL, {"agent_id": "alpha", "task_id": "t-1~handle"}),
        (TASK_CANCEL_TOOL, {"agent_id": "alpha", "task_id": "t-1~handle"}),
    ):
        asyncio.run(provider.execute(ctx, name, args))

    assert [call[0] for call in manager.calls] == ["call", "status", "cancel"]
    assert manager.scopes == [scope, scope, scope]


def test_the_task_id_parameters_ask_for_the_reported_handle():
    provider, _ = _provider(_FakeConnection("alpha"))
    connection = A2aRemoteAgentConnection(A2aRemoteAgentEntry(id="alpha", url="https://alpha.test/a2a"), DEPENDENCIES)

    descriptions = [
        provider.get(TASK_STATUS_TOOL, TEST_SCOPE).schema.parameters["properties"]["task_id"]["description"],
        provider.get(TASK_CANCEL_TOOL, TEST_SCOPE).schema.parameters["properties"]["task_id"]["description"],
        connection.schema.parameters["properties"]["task_id"]["description"],
    ]

    for description in descriptions:
        assert "exactly as" in description
        assert "reported" in description


def test_an_unknown_agent_tool_is_answered_not_raised():
    provider, _ = _provider(_FakeConnection("alpha"))
    assert _execute(provider, "a2a__ghost", {"message": "hi"}).startswith("⚠️ Unknown A2A tool")


def test_a_garbled_wait_sec_falls_back_to_a_status_read():
    provider, manager = _provider(_FakeConnection("alpha"), timeout=45)
    _execute(provider, TASK_STATUS_TOOL, {"agent_id": "alpha", "task_id": "t-1", "wait_sec": "soon"})
    assert manager.calls[-1] == ("status", "alpha", "t-1", 0.0, 45)


def test_dispatch_reaches_the_provider_through_the_catalog(tmp_path, monkeypatch):
    from ouroboros.policy import safety

    # The gates run for real; an unconfigured safety backend would prepend a warning.
    monkeypatch.setattr(safety, "check_safety", permissive_safety)

    provider, manager = _provider(_FakeConnection("alpha"))
    catalog = make_catalog([provider])
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    assert run_tool(catalog, ctx, "a2a__alpha", {"message": "hello"}) == "called alpha"
    assert manager.calls[-1][0] == "call"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_the_active_tool_gate_covers_remote_agents(tmp_path):
    provider, _manager = _provider(_FakeConnection("alpha"))
    catalog = make_catalog([provider])
    executor = ToolCallExecutor.__new__(ToolCallExecutor)
    executor._catalog = catalog

    assert executor._tool_inactive_for_task("a2a__alpha", frozenset({"data_read"}), TEST_SCOPE) is True
    assert executor._tool_inactive_for_task("a2a__alpha", frozenset({"a2a__alpha"}), TEST_SCOPE) is False
    # No restriction configured means no gate.
    assert executor._tool_inactive_for_task("a2a__alpha", None, TEST_SCOPE) is False


# ---------------------------------------------------------------------------
# Result classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("⚠️ A2A_ERROR (alpha): connection refused", "error"),
        ("⚠️ A2A_TIMEOUT (alpha): no result within 300s; task_id=t-1", "timeout"),
        ("⚠️ A2A_SUBMISSION_UNKNOWN (alpha): no answer to the send within 300s", "delivery_unknown"),
    ],
)
def test_a2a_failures_are_classified_as_tool_failures(result, status):
    assert ToolCallExecutor._is_tool_execution_failure(True, result, "a2a__alpha") is True
    meta = ToolCallExecutor._extract_result_metadata("a2a__alpha", result, True)
    assert meta["status"] == status


def test_an_unknown_submission_blocks_the_identical_retry_at_once():
    """A send that may already be running remotely is the one failure where a second identical
    call is worse than none: it is how one request becomes two remote tasks."""
    from ouroboros.agent.task_ledger import RepeatToolGuard

    result = "⚠️ A2A_SUBMISSION_UNKNOWN (alpha): no answer to the send within 300s"
    exec_result = {
        "result": result,
        "result_meta": ToolCallExecutor._extract_result_metadata("a2a__alpha", result, True),
    }
    failure_class = ToolCallExecutor._failure_class_for_result(exec_result)
    assert failure_class == "A2A_SUBMISSION_UNKNOWN"

    guard = RepeatToolGuard()
    args = {"message": "run the nightly report"}
    assert "retry will be blocked" in guard.record_failure("a2a__alpha", args, failure_class)
    assert guard.preflight("a2a__alpha", args).startswith("⚠️ TOOL_REPEAT_BLOCKED")
    # Only that exact call: asking after the task, or asking something else, still goes out.
    assert guard.preflight("a2a__alpha", {"message": "something else"}) == ""
    assert guard.preflight(TASK_STATUS_TOOL, {"agent_id": "alpha", "task_id": "t-1"}) == ""


def test_delegated_work_is_not_admitted_to_a_read_only_batch():
    """One batch can hold two calls to the same agent, or two continuations of one task, and
    what the far side does with them is unknown work — not a parallel-safe read."""
    provider, _ = _provider(_FakeConnection("alpha"))
    assert provider.get("a2a__alpha", TEST_SCOPE).parallel_safe is False
    assert provider.get(TASK_CANCEL_TOOL, TEST_SCOPE).parallel_safe is False
    # Reading a task's state is the one A2A call that changes nothing.
    assert provider.get(TASK_STATUS_TOOL, TEST_SCOPE).parallel_safe is True


def test_an_input_required_result_is_not_a_failure():
    result = "⚠️ A2A_INPUT_REQUIRED (alpha): Which repository?\ntask_id=t-1 — answer by calling this tool again."
    assert ToolCallExecutor._is_tool_execution_failure(True, result, "a2a__alpha") is False
    meta = ToolCallExecutor._extract_result_metadata("a2a__alpha", result, False)
    assert meta["status"] == "ok"


def test_a_successful_remote_answer_stays_successful():
    assert ToolCallExecutor._is_tool_execution_failure(True, "answer: 42", "a2a__alpha") is False
    assert ToolCallExecutor._extract_result_metadata("a2a__alpha", "answer: 42", False)["status"] == "ok"


def test_the_loop_records_an_a2a_error_as_a_failed_call(tmp_path, monkeypatch):
    class _FailingManager(_FakeManager):
        async def call_tool(self, scope, agent_id, arguments, timeout_sec):
            return "⚠️ A2A_ERROR (alpha): connection refused"

    results = _run_through_the_loop(tmp_path, monkeypatch, _FailingManager(_FakeConnection("alpha")), {"message": "hi"})

    assert results[0]["is_error"] is True
    assert results[0]["result_meta"]["status"] == "error"


def test_an_authorization_refusal_is_classified_as_a_failure(tmp_path, monkeypatch):
    refusal = (
        "⚠️ A2A_ERROR (alpha): cannot be authorized: no user token is stored for this owner and agent "
        "and the agent declares no static auth_token"
    )

    class _RefusingManager(_FakeManager):
        async def call_tool(self, scope, agent_id, arguments, timeout_sec):
            return refusal

    results = _run_through_the_loop(
        tmp_path, monkeypatch, _RefusingManager(_FakeConnection("alpha")), {"message": "hi"}
    )

    assert ToolCallExecutor._is_tool_execution_failure(True, refusal, "a2a__alpha") is True
    assert results[0]["is_error"] is True
    assert results[0]["result_meta"]["status"] == "error"
