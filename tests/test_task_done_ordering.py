"""Test that task_done is recorded only after send_message is queued.

The audit trail for task_done must go through EVENT_Q → supervisor _handle_task_done,
ensuring that send_message reaches the UI before task_done (causal ordering).
"""

import time
from types import SimpleNamespace


class TestTaskDoneOrdering:
    """Verify emit_task_results does not enqueue task_done before send_message."""

    def test_emit_task_results_does_not_record_task_done_directly(self, tmp_path):
        drive_root = tmp_path / "data"
        drive_root.mkdir()
        logs = drive_root / "logs"
        logs.mkdir()
        (drive_root / "memory").mkdir()
        (drive_root / "task_results").mkdir()
        from ouroboros.storage.runtime_store import init_runtime_store

        store = init_runtime_store(drive_root)

        env, ctx = SimpleNamespace(drive_root=drive_root), SimpleNamespace(pending_restart_reason=None)
        pending_events = []

        task = {"id": "test123", "type": "task", "chat_id": 1, "text": "hello"}
        usage = {"rounds": 3, "prompt_tokens": 100, "completion_tokens": 50}
        llm_trace = {"tool_calls": [], "reasoning_notes": []}

        import ouroboros.agent.task_pipeline as atp

        atp.emit_task_results(
            env=env,
            llm=object(),
            pending_events=pending_events,
            task=task,
            text="Reply text",
            usage=usage,
            llm_trace=llm_trace,
            start_time=time.time() - 1.0,
            drive_logs=logs,
            ctx=ctx,
        )

        event_types = [event.event_type for event in store.list_audit_events(log_stream="events", limit=20)]
        assert "task_done" not in event_types
        assert "task_eval" in event_types
        assert not (logs / "events.jsonl").exists()

    def test_pending_events_ordering(self, tmp_path):
        """Verify send_message comes before task_done in pending_events."""
        drive_root = tmp_path / "data"
        drive_root.mkdir()
        logs = drive_root / "logs"
        logs.mkdir()
        (drive_root / "memory").mkdir()
        (drive_root / "task_results").mkdir()

        env, ctx = SimpleNamespace(drive_root=drive_root), SimpleNamespace(pending_restart_reason=None)
        pending_events = []

        task = {"id": "order_test", "type": "task", "chat_id": 1, "text": "hi", "source": "browser"}
        usage = {"rounds": 1, "prompt_tokens": 50, "completion_tokens": 20}
        llm_trace = {"tool_calls": [], "reasoning_notes": []}

        import ouroboros.agent.task_pipeline as atp

        atp.emit_task_results(
            env=env,
            llm=object(),
            pending_events=pending_events,
            task=task,
            text="Order test reply",
            usage=usage,
            llm_trace=llm_trace,
            start_time=time.time() - 0.5,
            drive_logs=logs,
            ctx=ctx,
        )

        event_types = [e["type"] for e in pending_events]
        send_idx = event_types.index("send_message")
        done_idx = event_types.index("task_done")
        assert pending_events[send_idx]["source"] == "browser"
        assert pending_events[done_idx]["result"] == "Order test reply"
        assert send_idx < done_idx, (
            f"send_message (idx={send_idx}) must come before task_done (idx={done_idx}) "
            f"in pending_events to ensure causal ordering at the UI"
        )


class TestSupervisorTaskDoneAuditTrail:
    """Verify _handle_task_done records an event audit entry."""

    def test_handle_task_done_records_runtime_event(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        # Minimal context mock
        class MockCtx:
            DRIVE_ROOT = tmp_path
            WORKERS = {}
            executor = type("Exec", (), {"get_slot": staticmethod(lambda wid: None)})()

            def load_state(self):
                from ouroboros.storage.runtime_models import OwnerRuntimeState

                return OwnerRuntimeState()

            def save_state(self, state):
                pass

            def append_jsonl(self, path, data):
                from ouroboros.utils import append_jsonl

                append_jsonl(path, data)

        from supervisor.events import _handle_task_done

        evt = {
            "type": "task_done",
            "task_id": "test_td",
            "task_type": "task",
            "status": "failed",
            "total_rounds": 5,
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "ts": "2026-04-02T12:00:00Z",
        }
        from ouroboros.storage.runtime_store import init_runtime_store
        from ouroboros.storage.task_results import STATUS_FAILED, write_task_result

        init_runtime_store(tmp_path)
        write_task_result(tmp_path, "test_td", STATUS_FAILED, result="detailed failure")
        ctx = MockCtx()

        _handle_task_done(evt, ctx)

        store = init_runtime_store(tmp_path)
        task_done_entries = [
            event.metadata for event in store.list_audit_events(log_stream="events", event_type="task_done", limit=10)
        ]
        assert len(task_done_entries) == 1
        entry = task_done_entries[0]
        assert entry["task_id"] == "test_td"
        assert entry["total_rounds"] == 5
        assert store.get_task("test_td").result == "detailed failure"
        assert not (logs_dir / "events.jsonl").exists()

    def test_handle_task_done_cannot_complete_an_already_failed_task(self, tmp_path):
        class MockCtx:
            DRIVE_ROOT = tmp_path
            executor = type("Exec", (), {"get_slot": staticmethod(lambda wid: None)})()

        from ouroboros.storage.runtime_models import TaskRequest
        from ouroboros.storage.runtime_store import init_runtime_store
        from supervisor.events import _handle_task_done

        store = init_runtime_store(tmp_path)
        store.create_task(TaskRequest(id="late_td", type="task", text="run"), status="running")
        store.fail_task("late_td", result="Task killed by hard timeout after 1800s.")

        _handle_task_done(
            {
                "type": "task_done",
                "task_id": "late_td",
                "task_type": "task",
                "status": "completed",
                "result": "Finished, eventually.",
                "ts": "2026-04-02T12:00:00Z",
            },
            MockCtx(),
        )

        record = store.get_task("late_td")
        assert record.status == "failed"
        assert record.result == "Task killed by hard timeout after 1800s."

    def test_handle_task_heartbeat_forwards_live_log(self, tmp_path):
        captured: list[dict] = []

        class MockBridge:
            def push_log(self, payload):
                captured.append(dict(payload))

        class MockCtx:
            DRIVE_ROOT = tmp_path
            bridge = MockBridge()

        from ouroboros.storage.runtime_models import TaskRequest
        from ouroboros.storage.runtime_store import init_runtime_store
        from supervisor.events import _handle_task_heartbeat

        store = init_runtime_store(tmp_path)
        store.create_task(
            TaskRequest(id="heartbeat-1", type="task", text="run"),
            status="running",
        )

        _handle_task_heartbeat(
            {
                "type": "task_heartbeat",
                "task_id": "heartbeat-1",
                "phase": "running",
                "ts": "2026-04-02T12:00:01Z",
            },
            MockCtx(),
        )

        assert captured == [
            {
                "ts": "2026-04-02T12:00:01Z",
                "type": "task_heartbeat",
                "task_id": "heartbeat-1",
                "task_type": "task",
                "phase": "running",
            }
        ]
