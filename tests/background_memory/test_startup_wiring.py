"""What the composition root wires: the publisher, the loop, and the shutdown."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from background_memory.infra.events import memory_event_publisher_factory
from background_memory.infra.events.inmemory import InMemoryEventConsumer, MemoryEventQueue
from background_memory.interfaces import (
    ChatMessagePersistedEvent,
    ConsolidationContinuationEvent,
    FollowUp,
    MemoryDistillationEvent,
    MemoryTaskRef,
    NullMemoryEventPublisher,
)
from infra.async_engine import async_engine_factory
from tests.helpers.background_memory_db import llm_double, memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import bootstrap_memory_runtime_for_tests
from tests.helpers.user_settings import stub_user_settings


class _RecordingHandler:
    def __init__(self) -> None:
        self.handled = threading.Event()
        self.events: list[MemoryDistillationEvent] = []

    async def handle(self, event: MemoryDistillationEvent) -> FollowUp | None:
        self.events.append(event)
        self.handled.set()
        return None


@pytest.fixture
def db():
    yield from memory_database()


def _compose_the_process_engine(db) -> None:
    """``server.py`` reads the holder the lifespan composes; a test is its own root."""
    async_engine_factory.initialize(db.store.engine.url.render_as_string(hide_password=False))


@pytest.fixture(autouse=True)
def _clean_factory():
    reset_memory_event_publisher()
    yield
    reset_memory_event_publisher()


def _event(task_id: str = "src-live") -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        task=MemoryTaskRef(task_id=task_id, task_type="task", text="do it", recorded_at=1.5),
        response_text="A" * 200,
        llm_trace={"tool_calls": []},
    )


def test_a_published_event_reaches_the_handler_and_shutdown_ends_the_loop(db):
    handler = _RecordingHandler()
    parts = bootstrap_memory_runtime_for_tests(handlers={MemoryDistillationEvent: handler.handle})

    assert memory_event_publisher_factory.memory_publisher is parts.publisher

    running = asyncio.run_coroutine_threadsafe(parts.runtime.execute(), db.loop)
    try:
        memory_event_publisher_factory.memory_publisher.publish(_event())
        assert handler.handled.wait(5)
        assert [event.task.task_id for event in handler.events] == ["src-live"]
    finally:
        parts.stop_event.set()
        running.result(timeout=10)


def test_the_server_initializes_the_transport_and_starts_one_loop(monkeypatch, db):
    import server

    monkeypatch.setattr(server, "has_supervisor_provider", lambda _settings: True)

    async def _idle(_self, _runtime):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(server._MemoryRuntimeTask, "_run", _idle)
    monkeypatch.setattr(server, "_memory_handlers", dict)
    _compose_the_process_engine(db)
    runtime_task = server._MemoryRuntimeTask()
    monkeypatch.setattr(server, "_memory_runtime", runtime_task)

    async def _scenario() -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        started = server._start_memory_runtime_if_needed({}, loop=loop)
        # The duplicate-start guard: the same call while the loop lives is a no-op.
        again = server._start_memory_runtime_if_needed({}, loop=loop)
        await runtime_task.join(timeout=5)
        return started, again

    started, again = asyncio.get_event_loop().run_until_complete(_scenario())

    assert (started, again) == (True, False)
    assert not isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


def test_the_server_registers_both_handlers(monkeypatch, db):
    import server
    from background_memory.handlers.titles import PersistedMessageHandlers

    monkeypatch.setattr(server, "LLMClient", lambda: object())
    _compose_the_process_engine(db)

    handlers = server._memory_handlers()

    assert set(handlers) == {MemoryDistillationEvent, ChatMessagePersistedEvent, ConsolidationContinuationEvent}
    # The persisted-message kind carries two steps, the titling one first: it must inherit
    # neither the consolidation lease nor its thresholds.
    persisted = handlers[ChatMessagePersistedEvent].__self__
    assert isinstance(persisted, PersistedMessageHandlers)
    # One consolidation object behind both of its kinds: a continuation must find
    # the run the trigger left on it.
    assert persisted._consolidation is handlers[ConsolidationContinuationEvent].__self__
    assert persisted._consolidation._stop_event is server._stop_requested


def test_a_published_trigger_reaches_a_committed_summary(tmp_path, monkeypatch, db):
    """The whole path the composition root wires: publish, dispatch, commit, stop."""
    from background_memory.handlers import consolidation as consolidation_mod
    from background_memory.handlers.consolidation import ConsolidationHandler

    CONSOLIDATION_SCOPE = TEST_SCOPE
    from ouroboros.storage.runtime_models import ConversationMessage

    monkeypatch.setattr(
        consolidation_mod,
        "load_settings",
        lambda: SimpleNamespace(
            OUROBOROS_CHAT_SUMMARY_MAX_TOKENS=32_768,
            OUROBOROS_MODEL_CONTEXT_TOKENS=200_000,
            OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC=300,
            OUROBOROS_MODEL_FALLBACK="",
            OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_MESSAGES=1,
            OUROBOROS_CONTEXT_COMPACTION_THRESHOLD_TOKENS=25_000,
        ),
    )
    sql_conversation_repository(db.store.engine).append_message(
        ConversationMessage(
            message_id="message-0001",
            dedup_key="message-0001",
            owner_id=CONSOLIDATION_SCOPE.owner_id,
            agent_id=CONSOLIDATION_SCOPE.agent_id,
            chat_id=CONSOLIDATION_SCOPE.chat_id,
            actor_type="agent",
            content="the reply",
            created_at="2026-08-01T00:00:00+00:00",
            metadata={"chat_id": 1},
        ),
        scope=CONSOLIDATION_SCOPE,
    )
    llm = llm_double(lambda **kwargs: ({"content": "committed summary", "finish_reason": "stop"}, {}))
    handler = ConsolidationHandler(
        llm,
        leases=db.leases,
        summaries=db.summaries,
        history=db.history,
        audit=db.audit,
        user_settings=stub_user_settings(light_model="test-model", fallback_model=""),
        foreground_pressure=db.foreground_pressure(),
        stop_event=threading.Event(),
        holder_id="holder-a",
    )
    parts = bootstrap_memory_runtime_for_tests(
        handlers={ChatMessagePersistedEvent: handler.handle, ConsolidationContinuationEvent: handler.handle},
    )

    # On the loop the repositories' engine belongs to, which is where the roots run it.
    running = asyncio.run_coroutine_threadsafe(parts.runtime.execute(), db.loop)
    try:
        parts.publisher.publish(ChatMessagePersistedEvent(scope=CONSOLIDATION_SCOPE, message_id="message-0001"))
        deadline = time.monotonic() + 15
        while db.run(db.summaries.read(CONSOLIDATION_SCOPE)) is None:
            assert time.monotonic() < deadline, "the trigger never reached a commit"
            time.sleep(0.01)
    finally:
        parts.stop_event.set()
        running.result(timeout=15)
    committed = db.run(db.summaries.read(CONSOLIDATION_SCOPE))
    assert (committed.source_offsets_ref, committed.summary_ref) == ("message-0001", "committed summary")


def test_the_server_starts_no_loop_without_a_provider(monkeypatch):
    import server

    monkeypatch.setattr(server, "has_supervisor_provider", lambda _settings: False)
    runtime_task = server._MemoryRuntimeTask()
    monkeypatch.setattr(server, "_memory_runtime", runtime_task)

    assert server._start_memory_runtime_if_needed({}, loop=asyncio.get_event_loop()) is False
    assert runtime_task.is_alive() is False
    # Events are dropped by the null publisher, as they were before the seam existed.
    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)


def test_both_roots_register_the_same_kinds_and_the_same_composite(monkeypatch, tmp_path, db) -> None:
    """Two literals nothing compares: a kind added to one and not the other is a warning
    on a log nobody reads, in one deployment mode only."""
    import server
    from background_memory.handlers.titles import PersistedMessageHandlers
    from multiuser_config.background_memory import dependencies

    monkeypatch.setattr(server, "LLMClient", lambda: object())
    monkeypatch.setattr(dependencies, "LLMClient", lambda: object())
    monkeypatch.setattr(dependencies, "_resolved_consumer", lambda: InMemoryEventConsumer(MemoryEventQueue(1)))
    _compose_the_process_engine(db)

    single = server._memory_handlers()
    tier = dependencies.build_dependencies(
        drive_root=tmp_path,
        repo_dir=tmp_path,
        stop_event=threading.Event(),
        engine=db.engine,
    ).handlers

    assert set(single) == set(tier)
    assert isinstance(single[ChatMessagePersistedEvent].__self__, PersistedMessageHandlers)
    assert isinstance(tier[ChatMessagePersistedEvent].__self__, PersistedMessageHandlers)


# --- each root starts the loop and awaits it -----------------------------------


def test_the_server_root_starts_the_loop_and_awaits_it_on_the_way_out(monkeypatch, db):
    """A process must not report itself stopped while its deliveries are still running."""
    import server

    monkeypatch.setattr(server, "has_supervisor_provider", lambda _settings: True)
    monkeypatch.setattr(server, "_memory_handlers", dict)
    monkeypatch.setattr(server, "_stop_requested", threading.Event())
    _compose_the_process_engine(db)
    runtime_task = server._MemoryRuntimeTask()
    monkeypatch.setattr(server, "_memory_runtime", runtime_task)

    async def _scenario() -> bool:
        assert server._start_memory_runtime_if_needed({}, loop=asyncio.get_running_loop()) is True
        await asyncio.sleep(0.05)
        alive = runtime_task.is_alive()
        server._stop_requested.set()
        await runtime_task.join(timeout=5)
        return alive

    assert asyncio.get_event_loop().run_until_complete(_scenario()) is True
    assert runtime_task.is_alive() is False
    assert runtime_task.error is None


def test_the_tier_root_starts_the_loop_and_returns_once_it_has_drained(monkeypatch, db):
    """``main`` runs the loop to its stop; the docstring that said it needs no drain is gone."""
    from multiuser_config.background_memory import run as tier

    del db  # the tier composes its own engine; this only pins the store's drive root
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://models.example/v1")
    stop_event = threading.Event()
    monkeypatch.setattr(tier, "install_stop_signal", lambda event: stop_event.set() or event.set())
    monkeypatch.setattr(tier, "serve_probes", lambda _probes, **_kwargs: lambda: None)

    assert tier.main() == 0
    assert "needs no drain" not in (tier.__doc__ or "")
