"""Loop miscellaneous regressions.



Exercises narrow incoming-message behavior of the owner-inbox adapter.



Kept here as one module so future loop micro-regressions have a

natural home instead of producing yet another single-test file.

"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from ouroboros.agent.loop_adapters import OwnerInboxAdapter
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import supervisor_heartbeat
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from transport.interfaces import IInbox, InboundEnvelope


class _EmptyInbox(IInbox):
    """A required collaborator with nothing in it, polled without blocking."""

    def submit(self, envelope: InboundEnvelope) -> None:

        raise AssertionError("nothing submits here")

    def poll(self, timeout: float = 0.0) -> InboundEnvelope | None:

        del timeout

        return None


# ---------------------------------------------------------------------------

# OwnerInboxAdapter.drain — legacy image isolation

# ---------------------------------------------------------------------------


def test_drain_incoming_messages_preserves_image_payload():

    messages: list = []

    incoming_messages: queue.Queue = queue.Queue()

    incoming_messages.put(
        {
            "text": "photo from telegram",
            "image_base64": "aW1hZ2U=",
            "image_mime": "image/png",
            "image_caption": "photo from telegram",
        }
    )

    OwnerInboxAdapter(
        incoming_messages=incoming_messages,
        drive_root=None,
        task_id="",
        events=None,
        scope=TEST_SCOPE,
    ).drain(messages, set())

    assert len(messages) == 1

    assert messages[0]["role"] == "user"

    content = messages[0]["content"]

    assert content == "[Message from my human]: photo from telegram"

    assert "data:image" not in str(messages)

    assert "aW1hZ2U=" not in str(messages)


@pytest.mark.parametrize(
    ("env_value", "detached", "emits_progress"), [(None, False, False), ("true", False, True), ("true", True, False)]
)
def test_self_check_respects_checkpoint_progress_setting(monkeypatch, tmp_path, env_value, detached, emits_progress):

    from background_memory.interfaces import NullMemoryEventPublisher
    from ouroboros.agent.loop_adapters import EventSinkAdapter

    if env_value is None:
        monkeypatch.delenv("OUROBOROS_CHECKPOINT_PROGRESS_ENABLED", raising=False)

    else:
        monkeypatch.setenv("OUROBOROS_CHECKPOINT_PROGRESS_ENABLED", env_value)

    messages: list = []

    progress_calls: list = []

    events = RecordingAgentEvents()

    sink = EventSinkAdapter(
        emit_progress=progress_calls.append,
        events=events,
        task_id="",
        drive_logs=tmp_path,
        memory_events=NullMemoryEventPublisher(),
        scope=TEST_SCOPE,
        task={"detached": detached},
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    assert not sink.maybe_checkpoint([], round_idx=14, max_rounds=200)

    assert sink.maybe_checkpoint(messages, round_idx=15, max_rounds=200)

    assert "CHECKPOINT" in messages[0]["content"]

    assert bool(progress_calls) is emits_progress

    assert events.published[0].data["type"] == "task_checkpoint"


def test_build_user_content_exposes_only_safe_attachment_descriptor():

    from ouroboros.agent.context import build_user_content

    content = build_user_content(
        {
            "text": "Проверь вложение",
            "attachment_ids": ["att-1"],
            "attachments": [
                {
                    "id": "att-1",
                    "filename": "image.png",
                    "url": "api/chat/files/att-1",
                    "storage_key": "chat/att-1/image.png",
                }
            ],
        }
    )

    assert content.startswith("Проверь вложение")

    assert "att-1" in content

    assert "image.png" in content

    assert "send_file" in content

    assert "api/chat/files" not in str(content)

    assert "storage_key" not in str(content)


def test_supervisor_tick_does_not_run_attachment_io(monkeypatch, tmp_path):

    import supervisor.runtime as runtime

    monkeypatch.setattr(runtime, "rotate_chat_log_if_needed", lambda _root: None)

    monkeypatch.setattr(runtime, "_maybe_cleanup_working_retrieval", lambda _ctx: None)

    def _no_attachment_io(_provider, *, cancellation):
        raise AssertionError("attachment I/O ran in supervisor tick")

    monkeypatch.setattr(runtime, "cleanup_expired_attachments", _no_attachment_io)
    ctx = SimpleNamespace(
        drive_root=tmp_path,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        result_repo=runtime_persistence.current.result_repo,
        tasks_repo=runtime_persistence.current.tasks_repo,
        agent_events=SimpleNamespace(poll=lambda timeout=0.0: None),
        task_queue=SimpleNamespace(
            enforce_task_timeouts=lambda: None,
            reclaim_stale_leases=list,
            reconcile_parked_tasks=lambda confirmations: [],
        ),
    )

    stop_event = threading.Event()

    loop = runtime.SupervisorLoop(ctx=ctx, stop_event=stop_event, inbox=_EmptyInbox(), heartbeat=supervisor_heartbeat())

    assert loop.run_tick() == 0


def test_the_loop_beats_at_the_top_of_every_iteration(monkeypatch):

    import supervisor.runtime as runtime

    stop_event = threading.Event()

    heartbeat = supervisor_heartbeat()

    loop = runtime.SupervisorLoop(
        ctx=SimpleNamespace(), stop_event=stop_event, inbox=_EmptyInbox(), heartbeat=heartbeat
    )

    order: list[str] = []

    monkeypatch.setattr(heartbeat, "beat", lambda: order.append("beat"))

    def tick_then_stop() -> int:

        order.append("tick")

        stop_event.set()

        return 0

    monkeypatch.setattr(loop, "run_tick", tick_then_stop)

    loop.run()

    assert order == ["beat", "tick"], "a tick that hangs must leave the last beat ageing behind it"


def test_worker_loop_stops_with_its_owner(tmp_path):

    import supervisor.runtime as runtime

    stop_event = threading.Event()

    calls = []

    def body():

        calls.append(tmp_path)

        stop_event.set()

    runtime._worker_loop(body, stop_event, label="Test worker", trace_source="test-worker", interval=0)

    assert calls == [tmp_path]


def test_each_worker_pass_runs_under_its_own_operation():
    """A worker is process-lifetime with no caller, so it originates per pass.



    Unscoped it read an empty identifier and its S3 requests went out with no `X-Trace-Id`,

    which is what production showed: a checkpoint read, a `_meta/` listing and one GET per

    metadata object, none of them correlated with anything.

    """

    import supervisor.runtime as runtime
    from ouroboros.observability.aef_tracing import current_trace_id, normalize_trace_id

    stop_event = threading.Event()

    seen: list[str] = []

    def body():

        seen.append(current_trace_id())

        if len(seen) == 2:
            stop_event.set()

    runtime._worker_loop(body, stop_event, label="Test worker", trace_source="test-worker", interval=0)

    assert all(seen) and all(normalize_trace_id(value) == value for value in seen)

    # Two sweeps are two operations: a pass is the unit of work, not the process.

    assert seen[0] != seen[1]

    assert current_trace_id() == ""


def test_worker_thread_is_daemon():

    import supervisor.runtime as runtime

    thread = runtime._build_worker_thread(
        lambda: None,
        threading.Event(),
        name="attachment-cleanup",
        label="Attachment cleanup",
        trace_source="attachment-cleanup",
        interval=0,
    )

    assert thread.name == "attachment-cleanup"

    assert thread.daemon is True


def test_worker_shutdown_join_is_bounded(caplog):
    import supervisor.runtime as runtime

    stop_event = threading.Event()
    joined = []

    class Thread:
        def join(self, timeout=None):
            joined.append(timeout)

        def is_alive(self):
            return True

    runtime._stop_worker_thread(Thread(), stop_event, label="Attachment cleanup", timeout=1.0)

    assert stop_event.is_set()
    assert joined == [1.0]
    assert "still running" in caplog.text
