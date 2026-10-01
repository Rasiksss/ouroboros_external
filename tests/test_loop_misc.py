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
        event_queue=None,
    ).drain(messages, set())

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content == "[Message from my human]: photo from telegram"
    assert "data:image" not in str(messages)
    assert "aW1hZ2U=" not in str(messages)


@pytest.mark.parametrize(("env_value", "emits_progress"), [(None, False), ("true", True)])
def test_self_check_respects_checkpoint_progress_setting(monkeypatch, tmp_path, env_value, emits_progress):
    from ouroboros.agent.loop_adapters import EventSinkAdapter

    if env_value is None:
        monkeypatch.delenv("OUROBOROS_CHECKPOINT_PROGRESS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OUROBOROS_CHECKPOINT_PROGRESS_ENABLED", env_value)

    messages: list = []
    progress_calls: list = []
    events: queue.Queue = queue.Queue()
    sink = EventSinkAdapter(
        emit_progress=progress_calls.append,
        event_queue=events,
        task_id="",
        drive_logs=tmp_path,
    )

    assert not sink.maybe_checkpoint([], round_idx=14, max_rounds=200)
    assert sink.maybe_checkpoint(messages, round_idx=15, max_rounds=200)
    assert "CHECKPOINT" in messages[0]["content"]
    assert bool(progress_calls) is emits_progress
    assert events.get_nowait()["data"]["type"] == "task_checkpoint"


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
    monkeypatch.setattr(
        runtime,
        "_cleanup_expired_attachments",
        lambda _root: (_ for _ in ()).throw(AssertionError("attachment I/O ran in supervisor tick")),
    )
    executor = SimpleNamespace(
        ensure_healthy=lambda: None,
        get_event_q=queue.Queue,
        assign_next=lambda: None,
    )
    ctx = SimpleNamespace(
        drive_root=tmp_path,
        executor=executor,
        task_queue=SimpleNamespace(enforce_task_timeouts=lambda _executor: None),
    )
    loop = runtime.SupervisorLoop(
        ctx=ctx,
        bridge=SimpleNamespace(get_updates=lambda **_kwargs: []),
        stop_event=SimpleNamespace(is_set=lambda: False),
    )

    assert loop.run_tick(0) == 0


def test_attachment_cleanup_worker_stops_with_its_owner(monkeypatch, tmp_path):
    import supervisor.runtime as runtime

    stop_event = threading.Event()
    calls = []

    def cleanup(*_args, **_kwargs):
        calls.append(tmp_path)
        stop_event.set()

    monkeypatch.setattr(runtime, "_cleanup_expired_attachments", cleanup)

    runtime._attachment_cleanup_loop(
        SimpleNamespace(drive_root=tmp_path),
        stop_event,
    )

    assert calls == [tmp_path]


def test_attachment_cleanup_thread_is_daemon(tmp_path):
    import supervisor.runtime as runtime

    thread = runtime._build_attachment_cleanup_thread(
        SimpleNamespace(drive_root=tmp_path),
        threading.Event(),
    )

    assert thread.name == "attachment-cleanup"
    assert thread.daemon is True


def test_attachment_cleanup_shutdown_join_is_bounded(caplog):
    import supervisor.runtime as runtime

    stop_event = threading.Event()
    joined = []

    class Thread:
        def join(self, timeout=None):
            joined.append(timeout)

        def is_alive(self):
            return True

    runtime._stop_attachment_cleanup_thread(Thread(), stop_event)

    assert stop_event.is_set()
    assert joined == [1.0]
    assert "still running" in caplog.text
