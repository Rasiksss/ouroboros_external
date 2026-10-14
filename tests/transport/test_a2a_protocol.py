"""Tests for the A2A (Agent-to-Agent) protocol integration.

Covers: the runtime-database task store, A2A Executor, A2A Server (Agent Card, JSON-RPC),
response subscriptions on LocalChatBridge, and client tools.
"""

import asyncio
import hashlib
import logging
from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ouroboros.storage.chat_store import list_owner_messages
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.message_bus import LocalChatBridge, log_chat
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.helpers.usage import sql_usage_repository

# Requires a2a-sdk[http-server]==1.0.3 (see pyproject.toml).
pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")


from a2a.server.agent_execution.context import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import SendMessageRequest

from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents, SettlingReader
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import OuroborosA2AExecutor, derive_a2a_chat_id
from transport.a2a.interfaces import AgentSkillSpec, IAgentSkillCatalog, ISupervisorReadiness
from transport.a2a.proto import (
    new_task_for_test,
    new_user_text_message,
    task_state_name,
)
from transport.interfaces import InboundEnvelope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ReadyNow(ISupervisorReadiness):
    """The supervisor is already accepting submissions."""

    async def wait_ready(self, *, timeout_sec: float) -> None:
        return None


class _StubTaskStore:
    """What the executor asks the store, without the SDK's persistence."""

    def __init__(self, tasks=None):
        self.tasks = dict(tasks or {})

    async def get(self, task_id, context=None):
        del context
        return self.tasks.get(task_id)


class _StubCatalog(IAgentSkillCatalog):
    """The skills the composition root hands the card builder."""

    def __init__(self, specs=(), error=None):
        self._specs = list(specs)
        self._error = error

    def skills(self):
        if self._error is not None:
            raise self._error
        return list(self._specs)


def _make_executor(drive_root, *, max_concurrent=3, gateway=None, task_store=None, router=None):
    """Every collaborator is required, so every test names them."""
    gateway = gateway if gateway is not None else FakeGateway()
    return OuroborosA2AExecutor(
        max_concurrent=max_concurrent,
        task_store=task_store if task_store is not None else _StubTaskStore(),
        gateway=gateway,
        router=router if router is not None else A2AResponseRouter(gateway.outbox),
        readiness=_ReadyNow(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        drive_root=drive_root,
    )


# ===========================================================================
# 1. A2A task store
# ===========================================================================


class TestRuntimeDbTaskStore:
    """The one store production runs on: its round trip and its two reclaims."""

    def _make_store(self, tmp_path, ttl_hours=24):
        from transport.a2a.task_store import RuntimeDbA2ATaskStore

        runtime_persistence.initialize()
        return RuntimeDbA2ATaskStore(ttl_hours=ttl_hours)

    def test_save_and_get(self, tmp_path):
        store = self._make_store(tmp_path)
        asyncio.run(store.save(new_task_for_test("task-1", "completed", context_id="ctx-1")))

        loaded = asyncio.run(store.get("task-1"))
        assert loaded.id == "task-1"
        assert task_state_name(loaded.status.state) == "completed"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = self._make_store(tmp_path)

        assert asyncio.run(store.get("does-not-exist")) is None

    def test_save_overwrites(self, tmp_path):
        store = self._make_store(tmp_path)
        asyncio.run(store.save(new_task_for_test("task-1", "working", context_id="ctx-1")))
        asyncio.run(store.save(new_task_for_test("task-1", "completed", context_id="ctx-1")))

        assert task_state_name(asyncio.run(store.get("task-1")).status.state) == "completed"

    def test_cleanup_keeps_a_task_inside_its_ttl(self, tmp_path):
        store = self._make_store(tmp_path, ttl_hours=24)
        asyncio.run(store.save(new_task_for_test("task-1", "completed", context_id="ctx-1")))

        assert asyncio.run(store.cleanup_expired()) == 0

    def test_the_startup_sweep_fails_tasks_left_behind(self, tmp_path):
        store = self._make_store(tmp_path)
        asyncio.run(store.save(new_task_for_test("orphan", "working", context_id="ctx-1")))

        assert asyncio.run(store.fail_orphaned_tasks()) == 1
        assert task_state_name(asyncio.run(store.get("orphan")).status.state) == "failed"
        # Already terminal on the second pass, so it is not swept again.
        assert asyncio.run(store.fail_orphaned_tasks()) == 0

    def test_cleanup_reclaims_an_expired_non_terminal_task(self, tmp_path):
        store = self._make_store(tmp_path, ttl_hours=0)
        asyncio.run(store.save(new_task_for_test("orphan", "working", context_id="ctx-1")))

        assert asyncio.run(store.cleanup_expired()) == 1

    def test_a_protocol_row_is_never_claimed_as_queue_work(self, tmp_path):
        """A submitted exchange sits in ``tasks`` as ``queued`` and is not a task to run.

        It carries no input and no scope, so a worker that claimed it would hand the agent a
        payload with no identity — and the row it took is the protocol's own state.
        """
        from ouroboros.agent_runtime.executor import _UNCLAIMABLE_TASK_TYPES

        store = self._make_store(tmp_path)
        asyncio.run(store.save(new_task_for_test("submitted", "submitted", context_id="ctx-1")))

        record = runtime_persistence.current.tasks_repo.get_task("submitted")
        assert (record.status, record.task_type) == ("queued", "a2a")
        assert record.task_type in _UNCLAIMABLE_TASK_TYPES
        assert (
            runtime_persistence.current.tasks_repo.claim_next_task(
                worker_id=0, runtime_id="test-runtime", exclude_task_types=_UNCLAIMABLE_TASK_TYPES
            )
            is None
        )

    def test_a_row_with_no_stored_input_never_reports_an_empty_scope(self, tmp_path):
        """``scope: {}`` reads as an identity that is present and says nothing."""

        store = self._make_store(tmp_path)
        asyncio.run(store.save(new_task_for_test("submitted", "submitted", context_id="ctx-1")))

        assert "scope" not in runtime_persistence.current.tasks_repo.get_task("submitted").to_agent_input()


# Outbound delivery lives in tests/transport/test_a2a_outbound_delivery.py: the response
# subscriptions this section used to cover were replaced by a broadcast reader.


# ===========================================================================
# 2. A2A Executor
# ===========================================================================


class TestA2AExecutor:
    """OuroborosA2AExecutor unit tests."""

    def test_extract_text_from_text_part(self):
        from transport.a2a.executor import OuroborosA2AExecutor

        msg = new_user_text_message("hello")
        assert OuroborosA2AExecutor._extract_text(msg) == "hello"

    def test_extract_text_multiple_parts(self):
        from a2a.helpers.proto_helpers import new_message, new_text_part
        from a2a.types.a2a_pb2 import Role

        from transport.a2a.executor import OuroborosA2AExecutor

        msg = new_message(
            parts=[new_text_part("line1"), new_text_part("line2")],
            role=Role.ROLE_USER,
        )
        assert OuroborosA2AExecutor._extract_text(msg) == "line1\nline2"

    def test_extract_text_none_message(self):
        from transport.a2a.executor import OuroborosA2AExecutor

        assert OuroborosA2AExecutor._extract_text(None) == ""

    def test_extract_text_empty_parts(self):
        from a2a.types.a2a_pb2 import Message, Role

        from transport.a2a.executor import OuroborosA2AExecutor

        msg = Message(role=Role.ROLE_USER, message_id="m1")
        assert OuroborosA2AExecutor._extract_text(msg) == ""

    def test_concurrency_semaphore(self, tmp_path):
        executor = _make_executor(tmp_path, max_concurrent=2)
        assert executor._semaphore.acquire(blocking=False)
        assert executor._semaphore.acquire(blocking=False)
        assert not executor._semaphore.acquire(blocking=False)
        executor._semaphore.release()
        assert executor._semaphore.acquire(blocking=False)

    def test_chat_id_is_derived_from_the_task_id(self):
        """Derivation, not allocation: a retry must land in the same conversation."""
        from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
        from transport.a2a.executor import derive_a2a_chat_id

        first = derive_a2a_chat_id("task-abc")
        again = derive_a2a_chat_id("task-abc")
        other = derive_a2a_chat_id("task-def")

        assert first == again
        assert first != other
        for chat_id in (first, other):
            assert chat_id <= A2A_CHAT_ID_CEILING

    def test_an_empty_task_id_still_yields_a_usable_chat_id(self):
        from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
        from transport.a2a.executor import derive_a2a_chat_id

        first = derive_a2a_chat_id("")
        second = derive_a2a_chat_id("")

        assert first <= A2A_CHAT_ID_CEILING
        assert second <= A2A_CHAT_ID_CEILING
        # Nothing stable to hash, so each falls back to its own random seed.
        assert first != second

    def _make_context(self, text="hello", task_id="t1", context_id="c1"):
        """Build a minimal RequestContext-like object for testing."""
        from a2a.types.a2a_pb2 import Message, Role

        msg = new_user_text_message(text) if text else Message(role=Role.ROLE_USER, message_id="m1")

        class FakeContext:
            pass

        ctx = FakeContext()
        ctx.task_id = task_id
        ctx.context_id = context_id
        ctx.message = msg
        ctx.current_task = None
        return ctx

    def _make_event_queue(self):
        """A simple event collector implementing enqueue_event."""
        events = []

        class FakeEventQueue:
            async def enqueue_event(self, event):
                events.append(event)

        return FakeEventQueue(), events

    def test_execute_empty_message_fails(self, tmp_path):
        executor = _make_executor(tmp_path)
        ctx = self._make_context(text="")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        assert len(events) == 2
        assert hasattr(events[0], "status")
        assert task_state_name(events[1].status.state) == "failed"

    def test_execute_rejected_when_at_capacity(self, tmp_path):
        executor = _make_executor(tmp_path, max_concurrent=1)
        executor._semaphore.acquire()  # exhaust capacity
        ctx = self._make_context(text="hello")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        assert len(events) == 2
        assert task_state_name(events[1].status.state) == "rejected"
        executor._semaphore.release()

    def test_execute_success_with_mocked_supervisor(self, monkeypatch, tmp_path):
        from transport.a2a.executor import OuroborosA2AExecutor

        executor = _make_executor(tmp_path)

        async def fake_dispatch(self, text, tid, cid, mid=""):
            return "mocked response"

        monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", fake_dispatch)

        ctx = self._make_context(text="test question")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))

        states = [
            task_state_name(e.status.state) for e in events if hasattr(e, "status") and hasattr(e.status, "state")
        ]
        assert "working" in states
        assert "completed" in states
        artifacts = [e for e in events if hasattr(e, "artifact")]
        assert any(hasattr(e, "id") for e in events)
        assert len(artifacts) == 1
        assert artifacts[0].artifact.parts[0].text == "mocked response"

    def test_execute_failure_with_mocked_supervisor(self, monkeypatch, tmp_path):
        from transport.a2a.executor import OuroborosA2AExecutor

        executor = _make_executor(tmp_path)

        async def failing_dispatch(self, text, tid, cid, mid=""):
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", failing_dispatch)

        ctx = self._make_context(text="test")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))

        states = [task_state_name(e.status.state) for e in events if hasattr(e, "status")]
        assert "working" in states
        assert "failed" in states

    def test_execute_releases_semaphore_on_failure(self, monkeypatch, tmp_path):
        from transport.a2a.executor import OuroborosA2AExecutor

        executor = _make_executor(tmp_path, max_concurrent=1)

        async def failing_dispatch(self, text, tid, cid, mid=""):
            raise RuntimeError("boom")

        monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", failing_dispatch)

        ctx = self._make_context(text="test")
        eq, _events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        # Semaphore should be released — next acquire should succeed
        assert executor._semaphore.acquire(blocking=False)
        executor._semaphore.release()

    def test_cancel(self, tmp_path):
        executor = _make_executor(tmp_path)
        ctx = self._make_context(text="cancel me")
        eq, events = self._make_event_queue()
        asyncio.run(executor.cancel(ctx, eq))
        assert len(events) == 2
        assert task_state_name(events[1].status.state) == "canceled"


# ===========================================================================
# 4. A2A Server — Agent Card
# ===========================================================================


class TestAgentCard:
    """Dynamic Agent Card building."""

    def test_resolve_host_localhost(self):
        from transport.a2a.server import _resolve_host

        assert _resolve_host("127.0.0.1") == "127.0.0.1"
        assert _resolve_host("192.168.1.1") == "192.168.1.1"

    def test_resolve_host_wildcard(self):
        from transport.a2a.server import _resolve_host

        resolved = _resolve_host("0.0.0.0")
        assert resolved != "0.0.0.0"
        assert len(resolved) > 0

    def test_build_agent_card(self):
        from transport.a2a.server import _build_agent_card

        settings = {"A2A_AGENT_NAME": "", "A2A_AGENT_DESCRIPTION": ""}
        card = _build_agent_card(settings, "127.0.0.1", 18800, _StubCatalog())

        assert card.name == "ГигаАгент"
        assert card.description == "Personal AI agent"
        assert card.supported_interfaces[0].url == "http://127.0.0.1:18800/"
        assert card.capabilities.streaming is True
        assert len(card.skills) >= 1

    def test_build_agent_card_settings_override(self, tmp_path, monkeypatch):
        import ouroboros.app.config as config
        from transport.a2a.server import _build_agent_card

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)

        settings = {
            "A2A_AGENT_NAME": "CustomName",
            "A2A_AGENT_DESCRIPTION": "Custom description",
        }
        card = _build_agent_card(settings, "127.0.0.1", 18800, _StubCatalog())
        assert card.name == "CustomName"
        assert card.description == "Custom description"

    def test_build_agent_card_public_url_override(self, tmp_path, monkeypatch):
        from transport.a2a.server import _build_agent_card

        settings = {
            "A2A_AGENT_NAME": "",
            "A2A_AGENT_DESCRIPTION": "",
            "A2A_PUBLIC_URL": "http://agent-b:18800",
        }
        card = _build_agent_card(settings, "0.0.0.0", 18800, _StubCatalog())
        assert card.supported_interfaces[0].url == "http://agent-b:18800/"

    def test_the_card_publishes_the_injected_catalog(self):
        """The skills come from the collaborator the composition root passed in.

        Read off a process-global singleton instead, an instance with no agent
        published the fallback card and said nothing about it.
        """
        from transport.a2a.server import _card_skills

        skills = _card_skills(_StubCatalog([AgentSkillSpec("read_file", "read_file", "Reads a file", ("read",))]))

        assert [(skill.id, skill.name) for skill in skills] == [("read_file", "read_file")]
        assert skills[0].tags == ["read"]

    def test_an_empty_catalog_falls_back_and_stays_retryable(self):
        """Startup is usually the reason, so the next card request asks again."""
        from transport.a2a.server import _card_skills

        catalog = _StubCatalog()
        assert [skill.id for skill in _card_skills(catalog)] == ["general"]
        catalog._specs = [AgentSkillSpec("read_file", "read_file", "Reads a file", ("read",))]
        assert [skill.id for skill in _card_skills(catalog)] == ["read_file"]

    def test_a_failing_catalog_falls_back_and_says_so(self, caplog):
        from transport.a2a.server import _card_skills

        with caplog.at_level(logging.WARNING, logger="a2a-server"):
            skills = _card_skills(_StubCatalog(error=RuntimeError("catalog is gone")))

        assert [skill.id for skill in skills] == ["general"]
        assert "skill catalog failed" in caplog.text

    def test_the_card_modifier_republishes_live_skills(self):
        from transport.a2a.server import _build_agent_card, _refresh_skills

        catalog = _StubCatalog()
        card = _build_agent_card({}, "127.0.0.1", 18800, catalog)
        assert [skill.id for skill in card.skills] == ["general"]

        catalog._specs = [AgentSkillSpec("read_file", "read_file", "Reads a file", ("read",))]
        refreshed = _refresh_skills(card, catalog)

        assert [skill.id for skill in refreshed.skills] == ["read_file"]

    def test_stop_server_when_not_started(self):
        """stop_a2a_server should not raise when server was never started."""
        from transport.a2a.server import stop_a2a_server

        stop_a2a_server()  # should not raise

    def test_task_cleanup_loop_runs(self, tmp_path):
        """Verify cleanup loop calls store.cleanup_expired."""
        from transport.a2a.server import _task_cleanup_loop
        from transport.a2a.task_store import RuntimeDbA2ATaskStore

        runtime_persistence.initialize()
        store = RuntimeDbA2ATaskStore(ttl_hours=0)
        asyncio.run(store.save(new_task_for_test("old-task", "working", context_id="ctx")))

        # Run cleanup with very short interval, cancel after first run
        async def run_cleanup():
            task = asyncio.create_task(_task_cleanup_loop(store, interval_sec=0))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_cleanup())
        assert runtime_persistence.current.tasks_repo.get_task("old-task").status == "cancelled"


# ===========================================================================
# 5. A2A Config
# ===========================================================================


class TestA2AConfig:
    """A2A settings in SETTINGS_DEFAULTS."""

    def test_settings_defaults_include_a2a(self):
        from ouroboros.app.config import SETTINGS_DEFAULTS

        assert "A2A_ENABLED" in SETTINGS_DEFAULTS
        assert "A2A_PORT" in SETTINGS_DEFAULTS
        assert "A2A_HOST" in SETTINGS_DEFAULTS
        assert "A2A_AGENT_NAME" in SETTINGS_DEFAULTS
        assert "A2A_AGENT_DESCRIPTION" in SETTINGS_DEFAULTS
        assert "A2A_MAX_CONCURRENT" in SETTINGS_DEFAULTS
        assert "A2A_TASK_TTL_HOURS" in SETTINGS_DEFAULTS
        assert "A2A_PUBLIC_URL" in SETTINGS_DEFAULTS

    def test_default_values(self):
        from ouroboros.app.config import SETTINGS_DEFAULTS

        # A2A is disabled by default — enable in Settings -> Integrations
        assert SETTINGS_DEFAULTS["A2A_ENABLED"] is False
        assert SETTINGS_DEFAULTS["A2A_PORT"] == 18800
        assert SETTINGS_DEFAULTS["A2A_HOST"] == "127.0.0.1"
        assert SETTINGS_DEFAULTS["A2A_MAX_CONCURRENT"] == 3
        assert SETTINGS_DEFAULTS["A2A_TASK_TTL_HOURS"] == 24


# ===========================================================================
# Additional contract tests for executor active-task tracking and memory isolation
# ===========================================================================


class TestExecutorCancelTracking:
    """cancel() removes task from active set."""

    def _make_event_queue(self):
        events = []

        class _EQ:
            async def enqueue_event(self, ev):
                events.append(ev)

        return _EQ(), events

    def _make_context(self, task_id="t1"):
        return RequestContext(
            call_context=ServerCallContext(),
            request=SendMessageRequest(message=new_user_text_message("hello")),
            task_id=task_id,
            context_id="ctx1",
        )

    def test_cancel_removes_from_active_tasks(self, tmp_path):
        """cancel() discards task_id from _active_tasks."""
        import asyncio

        executor = _make_executor(tmp_path)
        # Manually add to simulate running task
        executor._active_tasks.add("t-cancel")
        ctx = self._make_context(task_id="t-cancel")
        eq, _ = self._make_event_queue()
        asyncio.run(executor.cancel(ctx, eq))
        assert "t-cancel" not in executor._active_tasks

    def test_execute_cleans_up_active_tasks_on_success(self, tmp_path):
        """execute() removes task_id from active set on completion."""
        import asyncio
        from unittest.mock import patch

        executor = _make_executor(tmp_path)
        ctx = self._make_context(task_id="t-exec")
        eq, _ = self._make_event_queue()

        async def _fake_dispatch(*a, **k):
            return "ok"

        with patch.object(executor, "_dispatch_to_supervisor", side_effect=_fake_dispatch):
            asyncio.run(executor.execute(ctx, eq))
        assert "t-exec" not in executor._active_tasks


class _AnsweringGateway(FakeGateway):
    """The supervisor's half of the seam, without a supervisor.

    A submission is recorded, optionally forwarded to a real orchestrator, and
    answered on the outbox — routed the way the reader thread routes it, so the
    waiting request completes without a background thread in the test.
    """

    def __init__(self, *, reply="agent answer", error=None, orchestrator=None):
        super().__init__()
        self.router = A2AResponseRouter(self.outbox)
        self._reply = reply
        self._error = error
        self._orchestrator = orchestrator

    @property
    def inbox(self):
        return self

    # -- IInbox ------------------------------------------------------------

    def submit(self, envelope):
        if self._error is not None:
            raise self._error
        super().inbox.submit(envelope)
        transport = envelope.transport
        reply = self._reply
        if self._orchestrator is not None:
            submission = self._orchestrator.submit(envelope)
            task = self._orchestrator.tasks_repo.get_owner_job(submission.task_id, owner_id=envelope.scope.owner_id)
            assert task is not None
            if submission.replay_result:
                reply = submission.message
            elif task.status == "queued" and reply is not None:
                self._orchestrator.tasks_repo.update_task_status(task.task_id, "completed", result=reply)
                transport = dict(task.input.get("transport") or {})
            else:
                return
        if reply is None:
            return
        LocalChatBridge(self.outbox).send_message(
            reply, scope=envelope.scope, source=envelope.source, transport=transport
        )
        answer = self.published[-1]
        reader = SettlingReader()
        self.router.handle(reader, reader.deliver(answer))

    def poll(self, timeout: float = 0.0):
        return super().inbox.poll(timeout)

    @property
    def submitted(self):
        return super().inbox.submitted


class TestA2AIngressPersistence:
    """The accepted request is recorded, and recording it can never break the call."""

    def _install(
        self, monkeypatch, tmp_path, *, reply="agent answer", error=None, hard_timeout=None, orchestrator=None
    ):
        """Wire the executor to a real store and a fake supervisor behind the seam."""
        import supervisor.message_bus as message_bus
        import supervisor.runtime_context as runtime_context
        import supervisor.state as supervisor_state
        from ouroboros.storage.runtime_models import OwnerRuntimeState
        from ouroboros.storage.settings.store import load_settings

        runtime_persistence.initialize()
        gateway = _AnsweringGateway(reply=reply, error=error, orchestrator=orchestrator)

        class _Runtime:
            drive_root = tmp_path

        state = OwnerRuntimeState(session_id="session-under-test")

        class _StateRepo:
            def load(self):
                return state

            def session_id(self):
                return str(state.session_id)

        _Runtime.state_repo = _StateRepo()
        monkeypatch.setattr(runtime_context, "try_get_runtime", lambda: _Runtime())
        monkeypatch.setattr(supervisor_state, "load_state", lambda: state)
        monkeypatch.setattr(message_bus, "_owner_session_id", lambda _scope: str(state.session_id))
        if hard_timeout is not None:
            settings = load_settings().model_copy(update={"OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout})
            monkeypatch.setattr("ouroboros.storage.settings.store.load_settings", lambda: settings)
        self._gateway = gateway
        self._drive_root = tmp_path
        return gateway

    def _executor(self, max_concurrent=3, task_store=None):
        return _make_executor(
            self._drive_root,
            max_concurrent=max_concurrent,
            gateway=self._gateway,
            task_store=task_store,
            router=self._gateway.router,
        )

    def _make_context(self, *, text="what is the deploy status", task_id="task-1", context_id="ctx-1"):
        from a2a.types.a2a_pb2 import Message, Role

        class FakeContext:
            pass

        ctx = FakeContext()
        ctx.task_id = task_id
        ctx.context_id = context_id
        ctx.message = new_user_text_message(text) if text else Message(role=Role.ROLE_USER, message_id="m1")
        ctx.current_task = None
        return ctx

    def _event_queue(self):
        events = []

        class _EQ:
            async def enqueue_event(self, event):
                events.append(event)

        return _EQ(), events

    def _execute(self, executor, ctx):
        eq, events = self._event_queue()
        asyncio.run(executor.execute(ctx, eq))
        return events

    def _context_scope(self, context_id: str = "ctx-1"):
        """The conversation shared by tasks in an A2A context."""
        from transport.a2a.executor import derive_a2a_chat_id

        return scope_for("a2a", derive_a2a_chat_id(context_id))

    def _owner_messages(self, tmp_path, context_id: str = "ctx-1"):
        from ouroboros.storage.chat_store import list_owner_messages

        rows = list_owner_messages(tmp_path, scope=self._context_scope(context_id))
        return [message for message in rows if message.actor_type == "owner"]

    def test_a_dispatched_message_reaches_history(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path)
        self._execute(self._executor(), self._make_context())

        messages = self._owner_messages(tmp_path)
        assert len(messages) == 1
        recorded = messages[0]
        assert recorded.content == "what is the deploy status"
        assert recorded.source == "a2a"
        assert recorded.metadata["task_id"] == "task-1"
        assert recorded.metadata["transport"] == {
            "a2a_context_id": "ctx-1",
            "a2a_task_id": "task-1",
            "correlation_id": self._gateway.submitted[-1].correlation_id,
        }
        assert recorded.metadata["sender_label"] == "a2a:ctx-1"

    def test_the_recorded_message_is_visible_in_history(self, monkeypatch, tmp_path):
        from ouroboros.storage.chat_store import list_chat_history_messages

        self._install(monkeypatch, tmp_path)
        self._execute(self._executor(), self._make_context())

        assert [m.content for m in list_chat_history_messages(tmp_path, limit=10, scope=self._context_scope())] == [
            "what is the deploy status"
        ]
        # And nowhere a browser client can name: the derived chat is negative.
        assert list_chat_history_messages(tmp_path, limit=10, scope=TEST_SCOPE) == []

    def test_redelivering_the_same_message_adds_no_second_row(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path)
        executor = self._executor()
        redelivered = self._make_context()
        self._execute(executor, redelivered)
        self._execute(executor, redelivered)

        assert len(self._owner_messages(tmp_path)) == 1

    @parametrize_from_dict(
        {
            "current retry": {"legacy": False, "new_task": False, "message_id": True},
            "current new message": {"legacy": False, "new_task": True, "message_id": True},
            "upgrade retry": {"legacy": "transport", "new_task": False, "message_id": True},
            "upgrade new message": {"legacy": "transport", "new_task": True, "message_id": True},
            "upgrade without message id": {"legacy": "transport", "new_task": False, "message_id": False},
            "upgrade supervisor history": {"legacy": "supervisor", "new_task": False, "message_id": True},
            "upgrade without history": {"legacy": "no history", "new_task": False, "message_id": True},
        }
    )
    def test_redelivery_reuses_history_when_available(self, monkeypatch, tmp_path, legacy, new_task, message_id):
        """Retries reuse recorded messages; new message IDs start their own tasks."""
        from supervisor.orchestrator.orchestrator import TaskOrchestrator
        from supervisor.queue import init as queue_init
        from supervisor.state import get_state_repository, init as state_init

        owners = sql_owner_state_repository()
        usage = sql_usage_repository()
        audit = sql_audit_event_repository()
        runtime_persistence.initialize()
        state_init(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        )
        orchestrator = TaskOrchestrator(
            conversations_repo=runtime_persistence.current.conversations_repo,
            tasks_repo=runtime_persistence.current.tasks_repo,
            create_root_task=runtime_persistence.current.create_root_task,
            task_queue=queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository()),
            state_repo=get_state_repository(
                tmp_path,
                owner_state_repository=owners,
                usage_repository=usage,
                audit_event_repository=audit,
            ),
            tool_confirmation_reader=sql_tool_confirmation_reader(),
        )
        self._install(monkeypatch, tmp_path, hard_timeout=1, orchestrator=orchestrator)

        executor = self._executor()
        redelivered = self._make_context()
        if not message_id:
            redelivered.message.message_id = ""
        scopes = {self._context_scope()}
        if legacy:
            chat_id = derive_a2a_chat_id(redelivered.task_id)
            envelope = InboundEnvelope(
                scope=scope_for("a2a", chat_id),
                user_id=chat_id,
                text="what is the deploy status",
                correlation_id=redelivered.task_id,
                client_message_id=redelivered.message.message_id
                or f"{redelivered.task_id}:{hashlib.sha256(b'what is the deploy status').hexdigest()[:16]}",
            )
            if legacy != "no history":
                log_chat(
                    "owner",
                    chat_id,
                    envelope.text,
                    scope=envelope.scope,
                    source="a2a",
                    client_message_id=envelope.client_message_id,
                    task_id=redelivered.task_id if legacy == "transport" else "",
                    transport={"a2a_context_id": redelivered.context_id} if legacy == "transport" else {},
                    extra_metadata={"inbound_processed": True},
                )
            submission = orchestrator.submit(envelope)
            runtime_persistence.current.tasks_repo.update_task_status(submission.task_id, "completed")
            runtime_persistence.current.tasks_repo.upsert_task(
                TaskRecord(
                    task_id=redelivered.task_id,
                    owner_id="a2a-task-store",
                    agent_id="main",
                    source="a2a",
                    task_type="a2a",
                    metadata={"a2a_payload": {"id": redelivered.task_id, "contextId": redelivered.context_id}},
                )
            )
            with runtime_persistence.current.engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    import_module("infra.migrations.versions.0033_a2a_context_identity").upgrade()
            assert list_owner_messages(tmp_path, scope=envelope.scope) == []
        else:
            self._execute(executor, redelivered)
        if new_task:
            redelivered.task_id = "task-2"
            redelivered.message.message_id = "message-for-task-2"
        events = self._execute(executor, redelivered)

        expected = ["what is the deploy status"] * (2 if new_task else 1)
        queued = [task for task in runtime_persistence.current.tasks_repo.list_tasks() if task.task_type == "task"]
        expected_tasks = ["what is the deploy status"] * (2 if new_task or legacy == "no history" else 1)
        assert [str(task.input.get("text")) for task in queued] == expected_tasks
        assert [
            message.content for scope in scopes for message in list_owner_messages(tmp_path, scope=scope)
        ] == expected
        assert task_state_name(events[-1].status.state) == "completed"

    def test_a_second_message_in_the_same_task_is_its_own_task(self, monkeypatch, tmp_path):
        """The other half of the same id: a turn is not a redelivery of the one before it."""
        from supervisor.orchestrator.orchestrator import TaskOrchestrator
        from supervisor.queue import init as queue_init
        from supervisor.state import get_state_repository, init as state_init

        owners = sql_owner_state_repository()
        usage = sql_usage_repository()
        audit = sql_audit_event_repository()
        runtime_persistence.initialize()
        state_init(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=audit,
        )
        self._install(
            monkeypatch,
            tmp_path,
            reply=None,
            hard_timeout=1,
            orchestrator=TaskOrchestrator(
                conversations_repo=runtime_persistence.current.conversations_repo,
                tasks_repo=runtime_persistence.current.tasks_repo,
                create_root_task=runtime_persistence.current.create_root_task,
                task_queue=queue_init(tmp_path, 10, 20, audit_event_repository=sql_audit_event_repository()),
                state_repo=get_state_repository(
                    tmp_path,
                    owner_state_repository=owners,
                    usage_repository=usage,
                    audit_event_repository=audit,
                ),
                tool_confirmation_reader=sql_tool_confirmation_reader(),
            ),
        )

        executor = self._executor()
        self._execute(executor, self._make_context(text="first question", context_id="task-1"))
        second = self._make_context(text="second question", context_id="task-1")
        self._execute(executor, second)

        queued = sorted(str(task.input.get("text")) for task in runtime_persistence.current.tasks_repo.list_tasks())
        assert queued == ["first question", "second question"]

    def test_a_second_message_in_the_same_task_is_its_own_row(self, monkeypatch, tmp_path):
        """A task can have several messages; deduplication must distinguish them."""
        self._install(monkeypatch, tmp_path)
        executor = self._executor()
        self._execute(executor, self._make_context(text="first question"))
        self._execute(executor, self._make_context(text="second question"))

        assert [m.content for m in self._owner_messages(tmp_path)] == ["first question", "second question"]

    @pytest.mark.parametrize("task_only", [False, True])
    def test_a_follow_up_task_reads_the_previous_exchange_in_its_context(self, monkeypatch, tmp_path, task_only):
        """New tasks and task-only continuations see both sides of the previous exchange."""
        from ouroboros.storage.chat_store import list_chat_history_messages
        from supervisor.message_bus import log_chat

        gateway = self._install(monkeypatch, tmp_path)
        executor = self._executor()
        self._execute(executor, self._make_context())

        first = gateway.submitted[-1]
        log_chat(
            "agent",
            first.chat_id,
            "agent answer",
            source="a2a",
            task_id="task-1",
            scope=first.scope,
        )
        follow_up_context = self._make_context(task_id="task-2", text="tell me more")
        if task_only:
            follow_up_context = RequestContext(
                call_context=ServerCallContext(),
                request=SendMessageRequest(message=new_user_text_message("tell me more", task_id="task-1")),
                task_id="task-1",
                task=new_task_for_test("task-1", "input_required", context_id="ctx-1"),
            )
        events = self._execute(executor, follow_up_context)

        follow_up = gateway.submitted[-1]
        exchange = list_chat_history_messages(tmp_path, limit=10, scope=follow_up.scope)
        assert [(message.actor_type, message.content) for message in reversed(exchange)] == [
            ("owner", "what is the deploy status"),
            ("agent", "agent answer"),
            ("owner", "tell me more"),
        ]
        assert list_chat_history_messages(tmp_path, limit=10, scope=TEST_SCOPE) == []
        assert all(event.context_id == "ctx-1" for event in events)

    @parametrize_from_dict(
        {
            "different contexts": {"first_context": "ctx-A", "second_context": "ctx-B"},
            "contextless tasks": {"first_context": "", "second_context": ""},
            "leading whitespace": {"first_context": "ctx-A", "second_context": " ctx-A"},
            "trailing whitespace": {"first_context": "ctx-A", "second_context": "ctx-A "},
        }
    )
    def test_different_contexts_or_contextless_tasks_have_isolated_history(
        self, monkeypatch, tmp_path, first_context, second_context
    ):
        from ouroboros.storage.chat_store import list_owner_messages

        self._install(monkeypatch, tmp_path)
        executor = self._executor()
        self._execute(executor, self._make_context(task_id="task-A", context_id=first_context, text="first"))
        self._execute(executor, self._make_context(task_id="task-B", context_id=second_context, text="second"))

        first_scope = self._context_scope(first_context or "task-A")
        second_scope = self._context_scope(second_context or "task-B")
        assert [m.content for m in list_owner_messages(tmp_path, scope=first_scope)] == ["first"]
        assert [m.content for m in list_owner_messages(tmp_path, scope=second_scope)] == ["second"]

    def test_progress_produced_for_a_remote_peer_stays_out_of_owner_history(self, monkeypatch, tmp_path):
        """The owner's browser asks for progress lines; the ones an agent emitted while
        serving somebody else's task are not theirs to read."""
        from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
        from transport.a2a.executor import derive_a2a_chat_id

        self._install(monkeypatch, tmp_path)
        a2a_scope = scope_for("a2a", derive_a2a_chat_id("task-1"))
        append_chat_message(
            tmp_path,
            scope=a2a_scope,
            actor_type="agent",
            chat_id=derive_a2a_chat_id("task-1"),
            user_id=0,
            text="internal progress for a remote peer",
            source="a2a",
            is_progress=True,
        )
        append_chat_message(
            tmp_path,
            scope=TEST_SCOPE,
            actor_type="agent",
            chat_id=1,
            user_id=0,
            text="thinking about your question",
            source="browser",
            is_progress=True,
        )

        visible = [m.content for m in list_owner_messages(tmp_path, include_progress=True, scope=TEST_SCOPE)]
        assert visible == ["thinking about your question"]
        assert list_owner_messages(tmp_path, include_progress=False, scope=TEST_SCOPE) == []
        # The carve-out is the second enforcement point: even read under its own conversation,
        # progress an agent emitted while serving a remote peer is nobody's owner-facing view.
        assert list_owner_messages(tmp_path, include_progress=True, scope=a2a_scope) == []

    def test_an_empty_message_records_nothing(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path)
        events = self._execute(self._executor(), self._make_context(text=""))

        assert task_state_name(events[-1].status.state) == "failed"
        assert self._owner_messages(tmp_path) == []

    def test_a_refused_request_records_nothing(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path)
        executor = self._executor(max_concurrent=1)
        executor._semaphore.acquire()
        events = self._execute(executor, self._make_context())
        executor._semaphore.release()

        assert task_state_name(events[-1].status.state) == "rejected"
        assert self._owner_messages(tmp_path) == []

    def test_a_failed_submission_keeps_its_row_unprocessed(self, monkeypatch, tmp_path):
        """The row is the journal a failed submit is replayed from, so it stays."""
        self._install(monkeypatch, tmp_path, error=RuntimeError("queue is down"))
        events = self._execute(self._executor(), self._make_context())

        assert task_state_name(events[-1].status.state) == "failed"
        (row,) = self._owner_messages(tmp_path)
        assert row.content == "what is the deploy status"
        assert row.metadata["inbound_processed"] is False

    def test_a_handed_over_request_is_marked_processed(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path)
        self._execute(self._executor(), self._make_context())

        (row,) = self._owner_messages(tmp_path)
        assert row.metadata["inbound_processed"] is True

    def test_a_cancelled_submission_keeps_its_record(self, monkeypatch, tmp_path):
        """Cancelling ``to_thread`` abandons the await, not the thread: the message may still
        reach the inbox, and a redelivery has to find the work it is asking after."""
        self._install(monkeypatch, tmp_path, error=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            self._execute(self._executor(), self._make_context())

        assert [m.content for m in self._owner_messages(tmp_path)] == ["what is the deploy status"]

    def test_the_delivery_id_is_the_same_in_history_and_in_the_envelope(self, monkeypatch, tmp_path):
        """One id, or the two deduplications disagree: history keeps one row while the
        supervisor queues a second task for the same request."""
        from ouroboros.storage.chat_store import get_chat_message_by_client_id
        from transport.a2a.executor import derive_a2a_chat_id

        gateway = self._install(monkeypatch, tmp_path)
        self._execute(self._executor(), self._make_context())

        (envelope,) = gateway.submitted
        assert envelope.client_message_id
        chat_id = derive_a2a_chat_id("ctx-1")
        # The row is addressable by the very id the envelope was submitted under.
        found = get_chat_message_by_client_id(
            tmp_path,
            scope=envelope.scope,
            actor_type="owner",
            source="a2a",
            chat_id=chat_id,
            user_id=chat_id,
            client_message_id=envelope.client_message_id,
        )
        assert found is not None
        assert found.message_id == self._owner_messages(tmp_path)[0].message_id

    def test_a_request_that_times_out_is_still_recorded(self, monkeypatch, tmp_path):
        self._install(monkeypatch, tmp_path, reply=None, hard_timeout=1)
        events = self._execute(self._executor(), self._make_context())

        assert task_state_name(events[-1].status.state) == "failed"
        assert [m.content for m in self._owner_messages(tmp_path)] == ["what is the deploy status"]

    def test_a_store_failure_is_swallowed(self, monkeypatch, tmp_path, caplog):
        import transport.a2a.executor as a2a_executor

        self._install(monkeypatch, tmp_path)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(a2a_executor, "append_chat_message", _explode)

        with caplog.at_level(logging.WARNING):
            events = self._execute(self._executor(), self._make_context())

        assert task_state_name(events[-1].status.state) == "completed"
        assert "database is gone" in caplog.text

    def test_a_task_left_working_by_a_dead_process_is_still_submittable(self, monkeypatch, tmp_path):
        """``_active_tasks`` is empty after a restart, so a stored ``working`` row is a leftover.

        Short-circuiting on it answered every retry with a status update and never
        submitted anything: that task id could never be answered again.
        """
        self._install(monkeypatch, tmp_path)
        store = _StubTaskStore({"task-1": new_task_for_test("task-1", "working", context_id="ctx-1")})

        events = self._execute(self._executor(task_store=store), self._make_context())

        assert task_state_name(events[-1].status.state) == "completed"
        assert [envelope.text for envelope in self._gateway.submitted] == ["what is the deploy status"]

    def test_a_task_this_process_is_running_is_not_submitted_twice(self, monkeypatch, tmp_path):
        """The retry wants the state of the work already asked for, not a second copy."""
        self._install(monkeypatch, tmp_path)
        executor = self._executor()
        executor._active_tasks.add("task-1")

        events = self._execute(executor, self._make_context())

        assert task_state_name(events[-1].status.state) == "working"
        assert self._gateway.submitted == []
        assert self._owner_messages(tmp_path) == []


class TestChatHistoryIsOneConversation:
    """Only progress messages are kept out of history; the chat decides which history.

    Covers: chat_store.py::render_chat_history and list_owner_messages (DB SSOT).
    """

    def test_a_synthetic_chat_id_hides_nothing_from_its_own_conversation(self, tmp_path):
        from ouroboros.storage.chat_store import list_owner_messages, render_chat_history
        from tests.conftest import seed_chat_entries

        human_entry = {"direction": "out", "text": "hello human", "ts": "2026-01-01T00:00:00", "chat_id": 1}
        a2a_entry = {
            "direction": "out",
            "text": "a2a response",
            "ts": "2026-01-01T00:00:01",
            "chat_id": -4001,
            "source": "a2a",
        }
        seed_chat_entries(tmp_path, [human_entry, a2a_entry])
        a2a_scope = scope_for("a2a", -4001)

        assert "a2a response" in render_chat_history(tmp_path, count=100, scope=a2a_scope)
        assert [m.content for m in list_owner_messages(tmp_path, scope=a2a_scope)] == ["a2a response"]
        # And the owner's own conversation carries theirs and nothing else.
        assert "a2a response" not in render_chat_history(tmp_path, count=100, scope=TEST_SCOPE)
        assert [m.content for m in list_owner_messages(tmp_path, scope=TEST_SCOPE)] == ["hello human"]

    def test_progress_messages_are_still_excluded(self, tmp_path):
        from ouroboros.storage.chat_store import append_chat_message, render_chat_history

        runtime_persistence.initialize()
        a2a_scope = scope_for("a2a", -4001)
        append_chat_message(
            tmp_path,
            scope=a2a_scope,
            actor_type="agent",
            chat_id=-4001,
            user_id=-4001,
            text="a2a progress",
            source="a2a",
            client_message_id="progress-1",
            is_progress=True,
        )

        assert "a2a progress" not in render_chat_history(tmp_path, count=100, scope=a2a_scope)

    def test_zero_chat_id_included(self, tmp_path):
        """chat_id=0 (or missing) entries pass the visibility filter (treated as human)."""
        from ouroboros.storage.chat_store import render_chat_history
        from tests.conftest import seed_chat_entries

        entry = {"direction": "out", "text": "system msg", "ts": "2026-01-01T00:00:00", "chat_id": 0}
        seed_chat_entries(tmp_path, [entry])
        result = render_chat_history(tmp_path, count=100, scope=scope_for("dialogs", 0))
        assert "system msg" in result


# ===========================================================================
# 6. The seam boundary and the executor's constructor
# ===========================================================================


class TestA2AStaysUnderTheSeam:
    """What a2a needs from above the transport layer arrives as a constructor argument."""

    def test_the_package_imports_nothing_above_the_seam(self):
        """The card's skills and the supervisor's readiness used to be imported directly."""
        import pathlib
        import re

        import transport.a2a

        forbidden = re.compile(r"^\s*(?:from|import)\s+(?:new_core|supervisor|ouroboros\.app|ouroboros\.agent)\b")
        # ``new_core.identity`` is the one exemption, matching ``_SEAM_SHARED`` in
        # ``tests/transport/test_transport_seam.py``: it is a leaf value module the seam has to be
        # able to name, and importing it pulls in no engine.
        exempt = re.compile(r"^\s*(?:from|import)\s+new_core\.identity\b")
        offenders = [
            f"{path.name}:{number}: {line.strip()}"
            for path in sorted(pathlib.Path(transport.a2a.__file__).parent.glob("*.py"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if forbidden.match(line) and not exempt.match(line)
        ]

        assert offenders == []

    def test_every_collaborator_is_required(self, tmp_path):
        """A fallback router is never started, and a missing drive root drops history."""
        gateway = FakeGateway()
        for missing in ("task_store", "router", "readiness", "drive_root", "max_concurrent"):
            kwargs = {
                "max_concurrent": 3,
                "task_store": _StubTaskStore(),
                "gateway": gateway,
                "router": A2AResponseRouter(gateway.outbox),
                "readiness": _ReadyNow(),
                "drive_root": tmp_path,
            }
            kwargs.pop(missing)
            with pytest.raises(TypeError):
                OuroborosA2AExecutor(**kwargs)

    def test_the_dispatch_takes_no_event_queue(self):
        """It never used it; the argument only made the call site lie about what it needs."""
        import inspect

        parameters = inspect.signature(OuroborosA2AExecutor._dispatch_to_supervisor).parameters

        assert list(parameters) == ["self", "text", "task_id", "context_id", "message_id"]
