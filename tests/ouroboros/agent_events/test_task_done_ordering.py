"""Test that the finish echo is reported only after the outbound message.

The observability row for a finished task is written by the component consuming the run's
reports, so the reports have to arrive in the order the UI needs: the assistant's reply
before the card collapses. The task's *status* is not on that path at all — the body wrote
it before publishing anything.
"""

import time
from types import SimpleNamespace

from ouroboros.agent_events.interfaces import OutboundText, TaskFinished, TaskHeartbeat
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE


class TestTaskDoneOrdering:
    """Verify emit_task_results does not enqueue task_done before send_message."""

    def test_emit_task_results_does_not_record_task_done_directly(self, tmp_path):
        drive_root = tmp_path / "data"
        drive_root.mkdir()
        logs = drive_root / "logs"
        logs.mkdir()
        (drive_root / "memory").mkdir()
        (drive_root / "task_results").mkdir()
        from ouroboros.storage.runtime_persistence import runtime_persistence

        runtime_persistence.initialize()

        env, ctx = (
            SimpleNamespace(drive_root=drive_root),
            SimpleNamespace(scope=TEST_SCOPE),
        )
        pending_events = []

        task = {"id": "test123", "type": "task", "chat_id": 1, "text": "hello"}
        usage = {"rounds": 3, "prompt_tokens": 100, "completion_tokens": 50}
        llm_trace = {"tool_calls": [], "reasoning_notes": []}

        import ouroboros.agent.task_pipeline as atp

        audit_repository = sql_audit_event_repository()
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
            scope=TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=audit_repository,
        )

        event_types = [event.event_type for event in audit_repository.list_audit_events(log_stream="events", limit=20)]
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

        env, ctx = (
            SimpleNamespace(drive_root=drive_root),
            SimpleNamespace(scope=TEST_SCOPE),
        )
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
            scope=TEST_SCOPE,
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )

        kinds = [type(event) for event in pending_events]
        send_idx = kinds.index(OutboundText)
        done_idx = kinds.index(TaskFinished)
        assert pending_events[send_idx].source == "browser"
        assert pending_events[done_idx].result == "Order test reply"
        assert send_idx < done_idx, (
            f"the outbound message (idx={send_idx}) must come before the finish echo "
            f"(idx={done_idx}) to ensure causal ordering at the UI"
        )


class TestSupervisorTaskDoneAuditTrail:
    """Verify _handle_task_done records an event audit entry."""

    def test_handle_task_done_records_runtime_event(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        audit_repository = sql_audit_event_repository()

        # Minimal context mock
        class MockCtx:
            DRIVE_ROOT = tmp_path
            state_repo = SimpleNamespace(audit_event_repository=audit_repository)
            WORKERS = {}
            executor = type("Exec", (), {"get_slot": staticmethod(lambda wid: None)})()

            def load_state(self):
                from ouroboros.storage.runtime_models import OwnerRuntimeState

                return OwnerRuntimeState()

            def save_state(self, state):
                pass

        from supervisor.events import _handle_task_done

        evt = TaskFinished(
            scope=TEST_SCOPE,
            task_id="test_td",
            task_type="task",
            status="failed",
            total_rounds=5,
            prompt_tokens=200,
            completion_tokens=80,
            ts="2026-04-02T12:00:00Z",
        )
        from ouroboros.storage.runtime_persistence import runtime_persistence
        from ouroboros.storage.task_results import STATUS_FAILED, write_task_result

        runtime_persistence.initialize()
        write_task_result(tmp_path, "test_td", STATUS_FAILED, scope=TEST_SCOPE, result="detailed failure")
        ctx = MockCtx()

        _handle_task_done(evt, ctx)

        store = runtime_persistence.initialize()
        task_done_entries = [
            event.metadata
            for event in audit_repository.list_audit_events(log_stream="events", event_type="task_done", limit=10)
        ]
        assert len(task_done_entries) == 1
        entry = task_done_entries[0]
        assert entry["task_id"] == "test_td"
        assert entry["total_rounds"] == 5
        assert store.tasks_repo.get_task("test_td").result == "detailed failure"
        assert not (logs_dir / "events.jsonl").exists()

    def test_handling_the_finish_echo_writes_no_status(self, tmp_path):
        """The consumer records the echo and nothing else: it did not witness the outcome."""

        audit_repository = sql_audit_event_repository()

        class MockCtx:
            DRIVE_ROOT = tmp_path
            state_repo = SimpleNamespace(audit_event_repository=audit_repository)

        from ouroboros.storage.runtime_models import TaskRequest
        from ouroboros.storage.runtime_persistence import runtime_persistence
        from supervisor.events import _handle_task_done

        store = runtime_persistence.initialize()
        store.tasks_repo.create_task(
            TaskRequest(id="late_td", type="task", text="run", scope=TEST_SCOPE.to_dict()), status="running"
        )
        store.tasks_repo.fail_task("late_td", result="Task killed by hard timeout after 1800s.")

        _handle_task_done(
            TaskFinished(
                scope=TEST_SCOPE,
                task_id="late_td",
                task_type="task",
                status="completed",
                result="Finished, eventually.",
                ts="2026-04-02T12:00:00Z",
            ),
            MockCtx(),
        )

        record = store.tasks_repo.get_task("late_td")
        assert record.status == "failed"
        assert record.result == "Task killed by hard timeout after 1800s."

        assert audit_repository.list_audit_events(event_type="task_done")[0].metadata["task_id"] == "late_td"

    def test_handle_task_heartbeat_forwards_live_log(self, tmp_path):
        captured: list[dict] = []

        class MockBridge:
            def push_log(self, payload):
                captured.append(dict(payload))

        class MockCtx:
            DRIVE_ROOT = tmp_path
            bridge = MockBridge()

        from supervisor.events import _handle_task_heartbeat

        # No record is written: the forward is observability and never reads the store.
        # The lease itself is written by the running body, on its own path.
        _handle_task_heartbeat(
            TaskHeartbeat(
                scope=TEST_SCOPE,
                task_id="heartbeat-1",
                task_type="task",
                phase="running",
                ts="2026-04-02T12:00:01Z",
            ),
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
