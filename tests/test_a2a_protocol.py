"""Tests for the A2A (Agent-to-Agent) protocol integration.

Covers: FileTaskStore, A2A Executor, A2A Server (Agent Card, JSON-RPC),
response subscriptions on LocalChatBridge, and client tools.
"""

import asyncio
import json
import logging

import pytest

# Requires a2a-sdk[http-server]==1.0.3 (see pyproject.toml).
pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")


from ouroboros.ingress.a2a_proto import (
    new_task_for_test,
    new_user_text_message,
    task_state_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_data_dir(tmp_path):
    """Return a temporary data directory mimicking ~/Ouroboros/data."""
    d = tmp_path / "data"
    d.mkdir()
    return d


# ===========================================================================
# 1. FileTaskStore
# ===========================================================================


class TestFileTaskStore:
    """File-based task persistence."""

    def _make_store(self, tmp_path, ttl_hours=24):
        from ouroboros.ingress.a2a_task_store import FileTaskStore

        return FileTaskStore(_tmp_data_dir(tmp_path), ttl_hours=ttl_hours)

    def _make_task(self, task_id="task-1", state="completed"):
        return new_task_for_test(task_id, state, context_id="ctx-1")

    def test_save_and_get(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        loaded = asyncio.run(store.get("task-1"))
        assert loaded is not None
        assert loaded.id == "task-1"
        assert task_state_name(loaded.status.state) == "completed"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = self._make_store(tmp_path)
        assert asyncio.run(store.get("does-not-exist")) is None

    def test_delete(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        asyncio.run(store.delete("task-1"))
        assert asyncio.run(store.get("task-1")) is None

    def test_delete_nonexistent_no_error(self, tmp_path):
        store = self._make_store(tmp_path)
        asyncio.run(store.delete("nope"))  # should not raise

    def test_save_overwrites(self, tmp_path):
        store = self._make_store(tmp_path)
        task1 = self._make_task(state="working")
        asyncio.run(store.save(task1))
        task2 = self._make_task(state="completed")
        asyncio.run(store.save(task2))
        loaded = asyncio.run(store.get("task-1"))
        assert task_state_name(loaded.status.state) == "completed"

    def test_atomic_write_creates_valid_json(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        task_file = store._dir / "task-1.json"
        assert task_file.exists()
        data = json.loads(task_file.read_text())
        assert data["id"] == "task-1"

    def test_safe_id_sanitization(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task(task_id="../../etc/passwd")
        asyncio.run(store.save(task))
        # Should not create files outside the task dir
        assert not (tmp_path / "etc").exists()
        path = store._task_path("../../etc/passwd")
        assert str(path).startswith(str(store._dir))

    def test_safe_id_nul_byte(self, tmp_path):
        """NUL bytes are collapsed to underscores."""
        store = self._make_store(tmp_path)
        path = store._task_path("task\x00../etc/passwd")
        assert str(path).startswith(str(store._dir))
        assert "\x00" not in str(path)

    def test_safe_id_absolute_path(self, tmp_path):
        """Absolute paths are confined to the task dir."""
        store = self._make_store(tmp_path)
        path = store._task_path("/etc/passwd")
        assert str(path).startswith(str(store._dir))

    def test_safe_id_windows_drive(self, tmp_path):
        """Windows drive-letter style IDs are safe."""
        store = self._make_store(tmp_path)
        path = store._task_path("C:\\Windows\\System32\\cmd.exe")
        assert str(path).startswith(str(store._dir))
        assert "Windows" not in str(path) or str(path).startswith(str(store._dir))

    def test_safe_id_only_dots(self, tmp_path):
        """IDs that resolve to only dots are replaced with invalid_task_id."""
        store = self._make_store(tmp_path)
        path = store._task_path("...")
        assert "invalid_task_id" in path.name

    def test_cleanup_expired_removes_old_terminal(self, tmp_path):
        import os
        import time

        store = self._make_store(tmp_path, ttl_hours=0)  # 0 = expire immediately
        task = self._make_task(state="completed")
        asyncio.run(store.save(task))
        # Backdate the file mtime
        task_file = store._dir / "task-1.json"
        old_time = time.time() - 3600
        os.utime(task_file, (old_time, old_time))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 1
        assert asyncio.run(store.get("task-1")) is None

    def test_cleanup_keeps_non_terminal(self, tmp_path):
        import os
        import time

        store = self._make_store(tmp_path, ttl_hours=0)
        task = self._make_task(state="working")
        asyncio.run(store.save(task))
        task_file = store._dir / "task-1.json"
        old_time = time.time() - 3600
        os.utime(task_file, (old_time, old_time))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 0
        assert asyncio.run(store.get("task-1")) is not None

    def test_cleanup_keeps_recent_terminal(self, tmp_path):
        store = self._make_store(tmp_path, ttl_hours=24)
        task = self._make_task(state="completed")
        asyncio.run(store.save(task))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 0

    def test_context_parameter_accepted(self, tmp_path):
        """TaskStore interface accepts optional ServerCallContext."""
        from a2a.server.context import ServerCallContext

        store = self._make_store(tmp_path)
        task = self._make_task()
        ctx = ServerCallContext()
        asyncio.run(store.save(task, ctx))
        loaded = asyncio.run(store.get("task-1", ctx))
        assert loaded is not None
        asyncio.run(store.delete("task-1", ctx))
        assert asyncio.run(store.get("task-1")) is None


# ===========================================================================
# 2. LocalChatBridge response subscriptions
# ===========================================================================


class TestBridgeSubscriptions:
    """Response subscription mechanism on LocalChatBridge."""

    def _make_bridge(self, monkeypatch):
        import supervisor.message_bus as mb

        monkeypatch.setattr(mb.LocalChatBridge, "_restart_telegram_polling", lambda self: None)
        return mb.LocalChatBridge({})

    def test_subscribe_and_receive(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        sub_id = bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "hello world")
        assert received == ["hello world"]
        bridge.unsubscribe_response(sub_id)

    def test_unsubscribe_stops_delivery(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        sub_id = bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.unsubscribe_response(sub_id)
        bridge.send_message(42, "should not arrive")
        assert received == []

    def test_subscription_filters_by_chat_id(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(99, "wrong chat_id")
        assert received == []

    def test_progress_messages_not_delivered(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "progress update", is_progress=True)
        assert received == []

    def test_multiple_subscribers(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        r1, r2 = [], []
        bridge.subscribe_response(42, lambda text: r1.append(text))
        bridge.subscribe_response(42, lambda text: r2.append(text))
        bridge.send_message(42, "broadcast")
        assert r1 == ["broadcast"]
        assert r2 == ["broadcast"]

    def test_callback_error_does_not_break_other_subscribers(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []

        def bad_callback(text):
            raise RuntimeError("boom")

        bridge.subscribe_response(42, bad_callback)
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "test")
        assert received == ["test"]

    def test_subscribe_returns_unique_ids(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        ids = set()
        for _ in range(100):
            ids.add(bridge.subscribe_response(1, lambda t: None))
        assert len(ids) == 100

    def test_unsubscribe_nonexistent_no_error(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        bridge.unsubscribe_response("does-not-exist")  # should not raise

    def test_negative_chat_id_works(self, monkeypatch):
        """A2A uses negative virtual chat_ids."""
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(-1001, lambda text: received.append(text))
        bridge.send_message(-1001, "a2a response")
        assert received == ["a2a response"]


# ===========================================================================
# 3. A2A Executor
# ===========================================================================


class TestA2AExecutor:
    """OuroborosA2AExecutor unit tests."""

    def test_extract_text_from_text_part(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        msg = new_user_text_message("hello")
        assert OuroborosA2AExecutor._extract_text(msg) == "hello"

    def test_extract_text_multiple_parts(self):
        from a2a.helpers.proto_helpers import new_message, new_text_part
        from a2a.types.a2a_pb2 import Role

        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        msg = new_message(
            parts=[new_text_part("line1"), new_text_part("line2")],
            role=Role.ROLE_USER,
        )
        assert OuroborosA2AExecutor._extract_text(msg) == "line1\nline2"

    def test_extract_text_none_message(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        assert OuroborosA2AExecutor._extract_text(None) == ""

    def test_extract_text_empty_parts(self):
        from a2a.types.a2a_pb2 import Message, Role

        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        msg = Message(role=Role.ROLE_USER, message_id="m1")
        assert OuroborosA2AExecutor._extract_text(msg) == ""

    def test_concurrency_semaphore(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=2)
        assert executor._semaphore.acquire(blocking=False)
        assert executor._semaphore.acquire(blocking=False)
        assert not executor._semaphore.acquire(blocking=False)
        executor._semaphore.release()
        assert executor._semaphore.acquire(blocking=False)

    def test_chat_id_is_derived_from_the_task_id(self):
        """Derivation, not allocation: a retry must land in the same conversation."""
        from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
        from ouroboros.ingress.a2a_executor import derive_a2a_chat_id

        first = derive_a2a_chat_id("task-abc")
        again = derive_a2a_chat_id("task-abc")
        other = derive_a2a_chat_id("task-def")

        assert first == again
        assert first != other
        for chat_id in (first, other):
            assert chat_id <= A2A_CHAT_ID_CEILING

    def test_an_empty_task_id_still_yields_a_usable_chat_id(self):
        from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_CEILING
        from ouroboros.ingress.a2a_executor import derive_a2a_chat_id

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

    def test_execute_empty_message_fails(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)
        ctx = self._make_context(text="")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        assert len(events) == 2
        assert hasattr(events[0], "status")
        assert task_state_name(events[1].status.state) == "failed"

    def test_execute_rejected_when_at_capacity(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=1)
        executor._semaphore.acquire()  # exhaust capacity
        ctx = self._make_context(text="hello")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        assert len(events) == 2
        assert task_state_name(events[1].status.state) == "rejected"
        executor._semaphore.release()

    def test_execute_success_with_mocked_supervisor(self, monkeypatch):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)

        async def fake_dispatch(self, text, eq, tid, cid, mid=""):
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

    def test_execute_failure_with_mocked_supervisor(self, monkeypatch):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)

        async def failing_dispatch(self, text, eq, tid, cid, mid=""):
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", failing_dispatch)

        ctx = self._make_context(text="test")
        eq, events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))

        states = [task_state_name(e.status.state) for e in events if hasattr(e, "status")]
        assert "working" in states
        assert "failed" in states

    def test_execute_releases_semaphore_on_failure(self, monkeypatch):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=1)

        async def failing_dispatch(self, text, eq, tid, cid, mid=""):
            raise RuntimeError("boom")

        monkeypatch.setattr(OuroborosA2AExecutor, "_dispatch_to_supervisor", failing_dispatch)

        ctx = self._make_context(text="test")
        eq, _events = self._make_event_queue()
        asyncio.run(executor.execute(ctx, eq))
        # Semaphore should be released — next acquire should succeed
        assert executor._semaphore.acquire(blocking=False)
        executor._semaphore.release()

    def test_cancel(self):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)
        ctx = self._make_context(text="cancel me")
        eq, events = self._make_event_queue()
        asyncio.run(executor.cancel(ctx, eq))
        assert len(events) == 2
        assert task_state_name(events[1].status.state) == "canceled"


# ===========================================================================
# 4. A2A Server — Agent Card
# ===========================================================================


def _seed_identity(content: str) -> None:
    """Put the identity content where the capability reads it: the memory row."""
    from ouroboros.storage.core_memory.memory import IDENTITY_NAME, get_memory
    from ouroboros.storage.core_memory.memory.interfaces.memory import MemoryRow

    memory = get_memory()
    memory._repo.upsert(
        MemoryRow(
            owner_id="global",
            name=IDENTITY_NAME,
            kind="identity",
            content=content,
            first_recorded_at=1.0,
            updated_at=1.0,
        )
    )


class TestAgentCard:
    """Dynamic Agent Card building."""

    def test_parse_identity_i_am_heading(self):
        from ouroboros.ingress.a2a_server import _parse_identity

        name, desc = _parse_identity("# I Am TestBot\n\nI do great things.\n")
        assert name == "TestBot"
        assert "great things" in desc

    def test_parse_identity_generic_heading(self):
        from ouroboros.ingress.a2a_server import _parse_identity

        name, desc = _parse_identity("# Who I Am\n\nI'm Ouroboros. I rewrite myself.\n")
        assert name == "Ouroboros"
        assert "rewrite" in desc

    def test_parse_identity_empty_file(self):
        from ouroboros.ingress.a2a_server import _parse_identity

        name, desc = _parse_identity("")
        assert name == ""
        assert desc == ""

    def test_identity_text_reads_the_memory_row(self):
        from ouroboros.ingress.a2a_server import _load_identity_text

        _seed_identity("# I Am TestBot\n\nI do great things.\n")
        assert _load_identity_text() == "# I Am TestBot\n\nI do great things.\n"

    def test_unreadable_identity_degrades_to_an_unnamed_card_and_says_so(self, monkeypatch, caplog):
        """An unnamed card and a headingless identity look identical from outside.

        Without the log line a permanently unnamed agent is invisible, so the
        traceback is the only thing separating the two.
        """
        from ouroboros.ingress.a2a_server import _load_identity_text, _parse_identity

        def _raise():
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr("ouroboros.ingress.a2a_server.get_memory", _raise)

        with caplog.at_level(logging.WARNING, logger="a2a-server"):
            text = _load_identity_text()

        assert text == ""
        assert _parse_identity(text) == ("", "")
        record = next(r for r in caplog.records if "identity document" in r.getMessage())
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None

    def test_building_the_card_does_no_memory_io(self, monkeypatch):
        """The card builder takes the text; the read happens off the event loop."""
        from ouroboros.ingress.a2a_server import _build_agent_card

        def _raise():
            raise AssertionError("_build_agent_card must not reach the memory capability")

        monkeypatch.setattr("ouroboros.ingress.a2a_server.get_memory", _raise)

        card = _build_agent_card({}, "127.0.0.1", 18800, "# I Am CardBot\n\nI build cards.\n")

        assert card.name == "CardBot"
        assert "build cards" in card.description

    def test_parse_identity_stops_at_hr(self):
        from ouroboros.ingress.a2a_server import _parse_identity

        _name, desc = _parse_identity("# I Am Bot\n\nFirst paragraph.\n\n---\n\nShould not appear.\n")
        assert "Should not appear" not in desc

    def test_parse_identity_stops_at_h2(self):
        from ouroboros.ingress.a2a_server import _parse_identity

        _name, desc = _parse_identity("# I Am Bot\n\nFirst paragraph.\n\n## Section\n\nHidden.\n")
        assert "Hidden" not in desc

    def test_resolve_host_localhost(self):
        from ouroboros.ingress.a2a_server import _resolve_host

        assert _resolve_host("127.0.0.1") == "127.0.0.1"
        assert _resolve_host("192.168.1.1") == "192.168.1.1"

    def test_resolve_host_wildcard(self):
        from ouroboros.ingress.a2a_server import _resolve_host

        resolved = _resolve_host("0.0.0.0")
        assert resolved != "0.0.0.0"
        assert len(resolved) > 0

    def test_build_agent_card(self):
        from ouroboros.ingress.a2a_server import _build_agent_card, _load_identity_text

        _seed_identity("# I Am TestAgent\n\nTest description.\n")

        settings = {"A2A_AGENT_NAME": "", "A2A_AGENT_DESCRIPTION": ""}
        card = _build_agent_card(settings, "127.0.0.1", 18800, _load_identity_text())

        assert card.name == "TestAgent"
        assert "Test description" in card.description
        assert card.supported_interfaces[0].url == "http://127.0.0.1:18800/"
        assert card.capabilities.streaming is True
        assert len(card.skills) >= 1

    def test_build_agent_card_settings_override(self, tmp_path, monkeypatch):
        import ouroboros.app.config as config
        from ouroboros.ingress.a2a_server import _build_agent_card

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)

        settings = {
            "A2A_AGENT_NAME": "CustomName",
            "A2A_AGENT_DESCRIPTION": "Custom description",
        }
        card = _build_agent_card(settings, "127.0.0.1", 18800, "")
        assert card.name == "CustomName"
        assert card.description == "Custom description"

    def test_build_agent_card_public_url_override(self, tmp_path, monkeypatch):
        from ouroboros.ingress.a2a_server import _build_agent_card

        settings = {
            "A2A_AGENT_NAME": "",
            "A2A_AGENT_DESCRIPTION": "",
            "A2A_PUBLIC_URL": "http://agent-b:18800",
        }
        card = _build_agent_card(settings, "0.0.0.0", 18800, "")
        assert card.supported_interfaces[0].url == "http://agent-b:18800/"

    def test_build_agent_card_falls_back_to_the_seeded_default_identity(self):
        """With no stored identity the default is seeded, so the card is still named."""
        from ouroboros.ingress.a2a_server import _build_agent_card, _load_identity_text

        settings = {"A2A_AGENT_NAME": "", "A2A_AGENT_DESCRIPTION": ""}
        card = _build_agent_card(settings, "127.0.0.1", 18800, _load_identity_text())
        assert card.name == "Ouroboros"
        assert "persistent agent" in card.description

    def test_build_agent_card_falls_back_to_settings_when_identity_is_unreadable(self, monkeypatch):
        """The hardcoded defaults only apply when identity cannot be read at all."""
        from ouroboros.ingress.a2a_server import _build_agent_card, _load_identity_text

        def _raise():
            raise RuntimeError("backend unreachable")

        monkeypatch.setattr("ouroboros.ingress.a2a_server.get_memory", _raise)

        settings = {"A2A_AGENT_NAME": "", "A2A_AGENT_DESCRIPTION": ""}
        card = _build_agent_card(settings, "127.0.0.1", 18800, _load_identity_text())
        assert card.name == "Ouroboros"
        assert card.description == "Personal AI agent"

    def test_build_skills_from_registry(self):
        """Returns skills read off the process-wide tool catalog, or the fallback."""
        from ouroboros.ingress.a2a_server import _build_skills_from_registry

        skills = _build_skills_from_registry()
        assert len(skills) >= 1
        # Either real tools or fallback "general"
        assert all(s.id for s in skills)
        assert all(s.name for s in skills)

    def test_build_skills_fallback_when_catalog_not_wired(self, monkeypatch):
        """An unwired tool catalog falls back to the single "general" skill.

        The fallback keys off ``tool_catalog.providers`` being empty — not the retired
        ``try_get_runtime`` the old test patched, which this code no longer calls. The cache
        is reset so a wired run earlier in the suite cannot answer for this one.
        """
        import types

        import ouroboros.ingress.a2a_server as a2a

        monkeypatch.setattr(a2a, "_SKILLS_CACHE", None)
        monkeypatch.setattr(a2a, "tool_catalog", types.SimpleNamespace(providers=()))

        skills = a2a._build_skills_from_registry()

        assert len(skills) == 1
        assert skills[0].id == "general"

    def test_stop_server_when_not_started(self):
        """stop_a2a_server should not raise when server was never started."""
        from ouroboros.ingress.a2a_server import stop_a2a_server

        stop_a2a_server()  # should not raise

    def test_task_cleanup_loop_runs(self, tmp_path):
        """Verify cleanup loop calls store.cleanup_expired."""
        import os
        import time as _time

        from ouroboros.ingress.a2a_server import _task_cleanup_loop
        from ouroboros.ingress.a2a_task_store import FileTaskStore

        store = FileTaskStore(_tmp_data_dir(tmp_path), ttl_hours=0)
        # Create an expired task
        task = new_task_for_test("old-task", "completed", context_id="ctx")
        asyncio.run(store.save(task))
        task_file = store._dir / "old-task.json"
        old_time = _time.time() - 7200
        os.utime(task_file, (old_time, old_time))

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
        assert asyncio.run(store.get("old-task")) is None


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
        from unittest.mock import MagicMock

        ctx = MagicMock()
        ctx.task_id = task_id
        ctx.context_id = "ctx1"
        ctx.message = new_user_text_message("hello")
        return ctx

    def test_cancel_removes_from_active_tasks(self):
        """cancel() discards task_id from _active_tasks."""
        import asyncio

        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)
        # Manually add to simulate running task
        executor._active_tasks.add("t-cancel")
        ctx = self._make_context(task_id="t-cancel")
        eq, _ = self._make_event_queue()
        asyncio.run(executor.cancel(ctx, eq))
        assert "t-cancel" not in executor._active_tasks

    def test_execute_cleans_up_active_tasks_on_success(self):
        """execute() removes task_id from active set on completion."""
        import asyncio
        from unittest.mock import patch

        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        executor = OuroborosA2AExecutor(max_concurrent=3)
        ctx = self._make_context(task_id="t-exec")
        eq, _ = self._make_event_queue()

        async def _fake_dispatch(*a, **k):
            return "ok"

        with patch.object(executor, "_dispatch_to_supervisor", side_effect=_fake_dispatch):
            asyncio.run(executor.execute(ctx, eq))
        assert "t-exec" not in executor._active_tasks


class _FakeBridge:
    """The subscription half of ``LocalChatBridge``, with a manual delivery hook."""

    def __init__(self):
        self.subscriptions = {}
        self._counter = 0

    def subscribe_response(self, chat_id, callback):
        self._counter += 1
        sub_id = f"sub-{self._counter}"
        self.subscriptions[sub_id] = (chat_id, callback)
        return sub_id

    def unsubscribe_response(self, sub_id):
        self.subscriptions.pop(sub_id, None)

    def deliver(self, text):
        for _chat_id, callback in list(self.subscriptions.values()):
            callback(text)


class _FakeOrchestrator:
    """Accepts an envelope and answers through the bridge, or refuses outright."""

    def __init__(self, bridge, *, reply="agent answer", error=None):
        self._bridge = bridge
        self._reply = reply
        self._error = error
        self.envelopes = []

    def submit(self, envelope):
        if self._error is not None:
            raise self._error
        self.envelopes.append(envelope)
        if self._reply is not None:
            self._bridge.deliver(self._reply)


class TestA2AIngressPersistence:
    """The accepted request is recorded, and recording it can never break the call."""

    def _install(self, monkeypatch, tmp_path, *, reply="agent answer", error=None, hard_timeout=None):
        """Wire the executor to a real store and fake supervisor internals."""
        import ouroboros.app.supervisor_readiness as readiness
        import supervisor.message_bus as message_bus
        import supervisor.runtime_context as runtime_context
        import supervisor.state as supervisor_state
        from ouroboros.storage.runtime_models import OwnerRuntimeState
        from ouroboros.storage.runtime_store import init_runtime_store
        from ouroboros.storage.settings.store import load_settings

        init_runtime_store(tmp_path)
        bridge = _FakeBridge()
        orchestrator = _FakeOrchestrator(bridge, reply=reply, error=error)

        class _Runtime:
            drive_root = tmp_path

        async def _ready(**_kwargs):
            return None

        state = OwnerRuntimeState(session_id="session-under-test")

        class _StateRepo:
            def load(self):
                return state

            def session_id(self):
                return str(state.session_id)

        _Runtime.state_repo = _StateRepo()
        monkeypatch.setattr(readiness, "wait_for_supervisor_ready", _ready)
        monkeypatch.setattr(message_bus, "try_get_bridge", lambda: bridge)
        monkeypatch.setattr(runtime_context, "try_get_orchestrator", lambda: orchestrator)
        monkeypatch.setattr(runtime_context, "try_get_runtime", lambda: _Runtime())
        monkeypatch.setattr(supervisor_state, "load_state", lambda: state)
        monkeypatch.setattr(message_bus, "load_state", lambda: state)
        monkeypatch.setattr(message_bus, "current_session_id", lambda: str(state.session_id))
        if hard_timeout is not None:
            settings = load_settings().model_copy(update={"OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout})
            monkeypatch.setattr("ouroboros.storage.settings.store.load_settings", lambda: settings)
        return bridge, orchestrator

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

    def _owner_messages(self, tmp_path):
        from ouroboros.storage.chat_store import list_owner_messages

        return [message for message in list_owner_messages(tmp_path) if message.actor_type == "owner"]

    def test_a_dispatched_message_reaches_history(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        messages = self._owner_messages(tmp_path)
        assert len(messages) == 1
        recorded = messages[0]
        assert recorded.content == "what is the deploy status"
        assert recorded.source == "a2a"
        assert recorded.metadata["task_id"] == "task-1"
        assert recorded.metadata["transport"] == {"a2a_context_id": "ctx-1"}
        assert recorded.metadata["sender_label"] == "a2a:ctx-1"

    def test_the_recorded_message_is_visible_in_history(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
        from ouroboros.storage.chat_store import list_chat_history_messages

        self._install(monkeypatch, tmp_path)
        self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert [m.content for m in list_chat_history_messages(tmp_path, limit=10)] == ["what is the deploy status"]

    def test_redelivering_the_same_message_adds_no_second_row(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        executor = OuroborosA2AExecutor(max_concurrent=3)
        redelivered = self._make_context()
        self._execute(executor, redelivered)
        self._execute(executor, redelivered)

        assert len(self._owner_messages(tmp_path)) == 1

    def test_redelivering_the_same_message_starts_no_second_task(self, monkeypatch, tmp_path):
        """The real orchestrator, because deduplicating history was never the whole job: the
        envelope has to carry the same id, since that is what the idempotent task id is
        derived from. Without it history held one row and the queue held two tasks."""
        import supervisor.runtime_context as runtime_context
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
        from ouroboros.storage.runtime_store import get_runtime_store
        from supervisor.orchestrator.orchestrator import TaskOrchestrator
        from supervisor.queue import init as queue_init
        from supervisor.state import get_state_repository, init as state_init

        # No reply: the caller times out, which is beside the point and keeps the test short.
        self._install(monkeypatch, tmp_path, reply=None, hard_timeout=1)
        state_init(tmp_path)
        orchestrator = TaskOrchestrator(
            store=get_runtime_store(tmp_path),
            task_queue=queue_init(tmp_path, 10, 20),
            state_repo=get_state_repository(tmp_path),
            agent_getter=lambda: None,
        )
        monkeypatch.setattr(runtime_context, "try_get_orchestrator", lambda: orchestrator)

        executor = OuroborosA2AExecutor(max_concurrent=3)
        redelivered = self._make_context()
        self._execute(executor, redelivered)
        self._execute(executor, redelivered)

        assert len(self._owner_messages(tmp_path)) == 1
        queued = get_runtime_store(tmp_path).list_tasks()
        assert [str(task.input.get("text")) for task in queued] == ["what is the deploy status"]

    def test_a_second_message_in_the_same_task_is_its_own_task(self, monkeypatch, tmp_path):
        """The other half of the same id: a turn is not a redelivery of the one before it."""
        import supervisor.runtime_context as runtime_context
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
        from ouroboros.storage.runtime_store import get_runtime_store
        from supervisor.orchestrator.orchestrator import TaskOrchestrator
        from supervisor.queue import init as queue_init
        from supervisor.state import get_state_repository, init as state_init

        self._install(monkeypatch, tmp_path, reply=None, hard_timeout=1)
        state_init(tmp_path)
        monkeypatch.setattr(
            runtime_context,
            "try_get_orchestrator",
            lambda: TaskOrchestrator(
                store=get_runtime_store(tmp_path),
                task_queue=queue_init(tmp_path, 10, 20),
                state_repo=get_state_repository(tmp_path),
                agent_getter=lambda: None,
            ),
        )

        executor = OuroborosA2AExecutor(max_concurrent=3)
        self._execute(executor, self._make_context(text="first question"))
        self._execute(executor, self._make_context(text="second question"))

        queued = sorted(str(task.input.get("text")) for task in get_runtime_store(tmp_path).list_tasks())
        assert queued == ["first question", "second question"]

    def test_a_second_message_in_the_same_task_is_its_own_row(self, monkeypatch, tmp_path):
        """A task is a conversation: keying history on the task id would swallow every turn
        after the first."""
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        executor = OuroborosA2AExecutor(max_concurrent=3)
        self._execute(executor, self._make_context(text="first question"))
        self._execute(executor, self._make_context(text="second question"))

        assert [m.content for m in self._owner_messages(tmp_path)] == ["first question", "second question"]

    def test_the_request_and_the_reply_share_one_conversation(self, monkeypatch, tmp_path):
        """The reply goes through the production writer, so the grouping is real."""
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor, derive_a2a_chat_id
        from ouroboros.storage.chat_store import list_owner_messages
        from supervisor.message_bus import log_chat

        self._install(monkeypatch, tmp_path)
        self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        chat_id = derive_a2a_chat_id("task-1")
        log_chat("agent", chat_id, chat_id, "agent answer", source="a2a", task_id="task-1")

        exchange = list_owner_messages(tmp_path)
        assert [message.actor_type for message in exchange] == ["owner", "agent"]
        assert len({message.conversation_id for message in exchange}) == 1
        assert exchange[0].conversation_id == f"a2a:{chat_id}"

    def test_different_tasks_are_different_conversations(self, monkeypatch, tmp_path):
        """A remote exchange has to be exportable and deletable on its own; grouping every
        task under the owner's session id makes that impossible."""
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
        from ouroboros.storage.chat_store import list_owner_messages

        self._install(monkeypatch, tmp_path)
        executor = OuroborosA2AExecutor(max_concurrent=3)
        self._execute(executor, self._make_context(task_id="task-A", text="first"))
        self._execute(executor, self._make_context(task_id="task-B", text="second"))

        conversations = {message.conversation_id for message in list_owner_messages(tmp_path)}
        assert len(conversations) == 2

    def test_progress_produced_for_a_remote_peer_stays_out_of_owner_history(self, monkeypatch, tmp_path):
        """The owner's browser asks for progress lines; the ones an agent emitted while
        serving somebody else's task are not theirs to read."""
        from ouroboros.ingress.a2a_executor import derive_a2a_chat_id
        from ouroboros.storage.chat_store import append_chat_message, list_owner_messages

        self._install(monkeypatch, tmp_path)
        append_chat_message(
            tmp_path,
            actor_type="agent",
            chat_id=derive_a2a_chat_id("task-1"),
            user_id=0,
            text="internal progress for a remote peer",
            source="a2a",
            is_progress=True,
        )
        append_chat_message(
            tmp_path,
            actor_type="agent",
            chat_id=1,
            user_id=0,
            text="thinking about your question",
            source="browser",
            is_progress=True,
        )

        visible = [m.content for m in list_owner_messages(tmp_path, include_progress=True)]
        assert visible == ["thinking about your question"]
        assert list_owner_messages(tmp_path, include_progress=False) == []

    def test_an_empty_message_records_nothing(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context(text=""))

        assert task_state_name(events[-1].status.state) == "failed"
        assert self._owner_messages(tmp_path) == []

    def test_a_refused_request_records_nothing(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        executor = OuroborosA2AExecutor(max_concurrent=1)
        executor._semaphore.acquire()
        events = self._execute(executor, self._make_context())
        executor._semaphore.release()

        assert task_state_name(events[-1].status.state) == "rejected"
        assert self._owner_messages(tmp_path) == []

    def test_a_failed_submission_records_nothing(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path, error=RuntimeError("queue is down"))
        events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert task_state_name(events[-1].status.state) == "failed"
        assert self._owner_messages(tmp_path) == []

    def test_a_submission_of_unknown_outcome_keeps_its_record(self, monkeypatch, tmp_path):
        """The task may be queued and only its commit record unreadable. Deleting the request
        there loses one the agent is about to answer — and a redelivery would find no trace of
        the work it is asking after."""
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
        from supervisor.orchestrator.orchestrator import SubmissionCommitUnknown

        self._install(monkeypatch, tmp_path, error=SubmissionCommitUnknown("task-1"))
        events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert task_state_name(events[-1].status.state) == "failed"
        assert [m.content for m in self._owner_messages(tmp_path)] == ["what is the deploy status"]

    def test_the_delivery_id_is_the_same_in_history_and_in_the_envelope(self, monkeypatch, tmp_path):
        """One id, or the two deduplications disagree: history keeps one row while the
        supervisor queues a second task for the same request."""
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor, derive_a2a_chat_id
        from ouroboros.storage.chat_store import get_chat_message_by_client_id

        _bridge, orchestrator = self._install(monkeypatch, tmp_path)
        self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        (envelope,) = orchestrator.envelopes
        assert envelope.client_message_id
        chat_id = derive_a2a_chat_id("task-1")
        # The row is addressable by the very id the envelope was submitted under.
        found = get_chat_message_by_client_id(
            tmp_path,
            actor_type="owner",
            source="a2a",
            chat_id=chat_id,
            user_id=chat_id,
            client_message_id=envelope.client_message_id,
        )
        assert found is not None
        assert found.message_id == self._owner_messages(tmp_path)[0].message_id

    def test_a_request_that_times_out_is_still_recorded(self, monkeypatch, tmp_path):
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path, reply=None, hard_timeout=1)
        events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert task_state_name(events[-1].status.state) == "failed"
        assert [m.content for m in self._owner_messages(tmp_path)] == ["what is the deploy status"]

    def test_a_store_failure_is_swallowed(self, monkeypatch, tmp_path, caplog):
        import supervisor.message_bus as message_bus
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("database is gone")

        monkeypatch.setattr(message_bus, "append_chat_message", _explode)

        with caplog.at_level(logging.WARNING):
            events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert task_state_name(events[-1].status.state) == "completed"
        assert "database is gone" in caplog.text

    def test_no_runtime_skips_persistence_without_raising(self, monkeypatch, tmp_path):
        import supervisor.runtime_context as runtime_context
        from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor

        self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(runtime_context, "try_get_runtime", lambda: None)

        events = self._execute(OuroborosA2AExecutor(max_concurrent=3), self._make_context())

        assert task_state_name(events[-1].status.state) == "completed"
        assert self._owner_messages(tmp_path) == []


class TestChatHistoryHoldsA2ATraffic:
    """A2A traffic belongs to chat history; only progress messages are kept out.

    Covers: chat_store.py::render_chat_history and list_owner_messages (DB SSOT).
    """

    def test_negative_chat_id_is_visible(self, tmp_path):
        from ouroboros.storage.chat_store import list_owner_messages, render_chat_history
        from tests.conftest import seed_chat_entries

        human_entry = {"direction": "out", "text": "hello human", "ts": "2026-01-01T00:00:00", "chat_id": 1}
        a2a_entry = {"direction": "out", "text": "a2a response", "ts": "2026-01-01T00:00:01", "chat_id": -4001}
        seed_chat_entries(tmp_path, [human_entry, a2a_entry])

        result = render_chat_history(tmp_path, count=100)
        assert "hello human" in result
        assert "a2a response" in result
        assert "a2a response" in [message.content for message in list_owner_messages(tmp_path)]

    def test_progress_messages_are_still_excluded(self, tmp_path):
        from ouroboros.storage.chat_store import append_chat_message, render_chat_history
        from ouroboros.storage.runtime_store import init_runtime_store

        init_runtime_store(tmp_path)
        append_chat_message(
            tmp_path,
            actor_type="agent",
            chat_id=-4001,
            user_id=-4001,
            text="a2a progress",
            source="a2a",
            client_message_id="progress-1",
            is_progress=True,
        )

        assert "a2a progress" not in render_chat_history(tmp_path, count=100)

    def test_zero_chat_id_included(self, tmp_path):
        """chat_id=0 (or missing) entries pass the filter (treated as human)."""
        from ouroboros.storage.chat_store import render_chat_history
        from tests.conftest import seed_chat_entries

        entry = {"direction": "out", "text": "system msg", "ts": "2026-01-01T00:00:00", "chat_id": 0}
        seed_chat_entries(tmp_path, [entry])
        result = render_chat_history(tmp_path, count=100)
        assert "system msg" in result
