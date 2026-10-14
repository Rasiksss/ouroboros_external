from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros.agent_events.interfaces import TaskFinished
from ouroboros.storage.chat_store import list_owner_messages
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.blob_double import recording_blob_double
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser import ws as browser_ws
from transport.browser.contracts import WS_MESSAGE_TYPES
from transport.infra.inmemory import InProcessOutbox
from transport.interfaces import IEnvelopeBuilder, OutboundKind, OutboundMessage, SubscriptionMode

WS_OUTBOUND_FIXTURES: dict[str, dict] = {
    "chat": {
        "type": "chat",
        "role": "assistant",
        "content": "hello",
        "ts": "2026-01-01T00:00:00+00:00",
    },
    "photo": {
        "type": "photo",
        "role": "assistant",
        "image_base64": "aGVsbG8=",
        "mime": "image/png",
        "ts": "2026-01-01T00:00:00+00:00",
    },
    "typing": {"type": "typing", "action": "typing"},
    "log": {"type": "log", "data": {"level": "info", "message": "worker started"}},
    "heartbeat": {"type": "heartbeat", "ts": "2026-01-01T00:00:00+00:00"},
    "extension_lifecycle": {
        "type": "extension_lifecycle",
        "skill": "demo",
        "action": "load",
        "status": "ok",
    },
    "confirmation_request": {
        "type": "confirmation_request",
        "confirmation_id": "ab" * 16,
        "task_id": "task-1",
        "tool": "send_file",
        "arguments_preview": '{"file_path": "report.pdf"}',
        "arguments_truncated": False,
        "text": "Подтвердите действие: send_file [abababab]",
        "ts": "2026-01-01T00:00:00+00:00",
    },
}

WS_INBOUND_FIXTURES: dict[str, dict] = {
    "chat": {"type": "chat", "content": "hello"},
    "command": {"type": "command", "cmd": "/help"},
}


def _required_keys(msg_type: str, payload: dict) -> set[str]:
    if msg_type == "chat":
        return {"type", "role", "content", "ts"}
    if msg_type == "photo":
        return {"type", "role", "image_base64", "mime", "ts"}
    if msg_type == "typing":
        return {"type", "action"}
    if msg_type == "log":
        return {"type", "data"}
    if msg_type == "heartbeat":
        return {"type"}
    if msg_type == "extension_lifecycle":
        return {"type"}
    if msg_type == "command":
        return {"type", "cmd"}
    if msg_type == "confirmation_request":
        return {"type", "confirmation_id", "tool", "arguments_preview", "text"}
    return {"type"}


@pytest.mark.parametrize("msg_type", WS_MESSAGE_TYPES)
def test_ws_message_type_parity_fixtures(msg_type: str):
    assert msg_type in WS_OUTBOUND_FIXTURES or msg_type in WS_INBOUND_FIXTURES
    payload = WS_OUTBOUND_FIXTURES.get(msg_type) or WS_INBOUND_FIXTURES[msg_type]
    assert _required_keys(msg_type, payload).issubset(payload.keys())
    assert payload["type"] == msg_type


def _browser_ws_client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True)


def _browser_runtime(monkeypatch, tmp_path, *, submit=None):
    """A runtime whose gateway the app will share; ``submit`` observes the seam."""
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    if submit is not None:
        monkeypatch.setattr(runtime.gateway.inbox, "submit", submit)
    return runtime


def _upload_text(client):
    return client.post(
        "/api/chat/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
    ).json()


def test_ws_chat_is_accepted_before_the_supervisor_exists(monkeypatch, tmp_path):
    """The transport hands the message to the seam; who consumes it is not its business."""
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello"})
            frame = json.loads(websocket.receive_text())

        assert frame["role"] == "user"
        assert frame["content"] == "hello"
        assert [message["text"] for message in client.get("/api/chat/history").json()["messages"]] == ["hello"]


def test_ws_chat_submits_through_the_inbox(monkeypatch, tmp_path):
    submitted: list[dict] = []
    runtime = bootstrap_supervisor_for_tests(tmp_path)

    def _capture_submit(envelope):
        submitted.append(
            {
                "text": envelope.effective_text(),
                "source": envelope.source,
                "chat_id": envelope.chat_id,
                "detached": envelope.detached,
            }
        )

    monkeypatch.setattr(runtime.gateway.inbox, "submit", _capture_submit)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "queue me",
                    "sender_session_id": "sess-1",
                    "client_message_id": "c-1",
                }
            )
            user_frame = json.loads(websocket.receive_text())
            assert user_frame["role"] == "user"
            assert user_frame["source"] == "browser"
            deadline = time.time() + 2.0
            while not submitted and time.time() < deadline:
                time.sleep(0.01)
            assert submitted == [
                {
                    "text": "queue me",
                    "source": "browser",
                    "chat_id": str(browser_ws.BROWSER_CHAT_ID),
                    "detached": False,
                }
            ]


def test_ws_detached_acceptance_uses_the_assistant_chat_shape_with_public_job_id(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    submitted = []

    def submit(envelope):
        submitted.append(envelope)
        runtime.gateway.outbox.publish(
            OutboundMessage(
                scope=envelope.scope,
                source="browser",
                text="Job logical-job queued.",
                task_id="logical-job",
            )
        )

    monkeypatch.setattr(runtime.gateway.inbox, "submit", submit)
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {"type": "chat", "content": "durable work", "client_message_id": "detached-1", "detached": True}
            )
            frames = [json.loads(websocket.receive_text()), json.loads(websocket.receive_text())]

    assistant = next(frame for frame in frames if frame.get("role") == "assistant")
    assert assistant["content"] == "Job logical-job queued."
    assert assistant["task_id"] == "logical-job"
    assert len(submitted) == 1 and submitted[0].detached is True


def test_ws_chat_submits_when_runtime_state_is_unavailable(monkeypatch, tmp_path):
    submitted: list[str] = []
    runtime = bootstrap_supervisor_for_tests(tmp_path)

    def _load_failed():
        raise OSError("state unavailable")

    def _capture_submit(envelope):
        submitted.append(envelope.effective_text())

    monkeypatch.setattr(runtime.state_repo, "load", _load_failed)
    monkeypatch.setattr(runtime.gateway.inbox, "submit", _capture_submit)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "still queue me"})
            frame = json.loads(websocket.receive_text())
            assert frame["role"] == "user"

            deadline = time.time() + 2.0
            while not submitted and time.time() < deadline:
                time.sleep(0.01)

    assert submitted == ["still queue me"]


def test_ws_chat_accepts_attachment_only_message(monkeypatch, tmp_path):
    submitted: list[dict] = []

    def _capture_submit(envelope):
        submitted.append(
            {
                "text": envelope.text,
                "attachment_ids": envelope.attachment_ids,
                "image_base64": envelope.image_base64,
                "detached": envelope.detached,
            }
        )
        return SimpleNamespace(intent="chat")

    _browser_runtime(monkeypatch, tmp_path, submit=_capture_submit)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "",
                    "attachment_ids": [upload["attachment_id"]],
                    "detached": True,
                }
            )
            frame = json.loads(websocket.receive_text())
            deadline = time.time() + 2.0
            while not submitted and time.time() < deadline:
                time.sleep(0.01)
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "reuse",
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            replay = json.loads(websocket.receive_text())

    assert frame["attachments"][0]["id"] == upload["attachment_id"]
    assert replay["attachments"][0]["id"] == upload["attachment_id"]
    assert submitted[0] == {
        "text": "",
        "attachment_ids": [upload["attachment_id"]],
        "image_base64": "",
        "detached": True,
    }


@pytest.mark.parametrize("detached", [None, 0, 1, "true", [], {}])
def test_ws_chat_rejects_a_non_boolean_detached_value(monkeypatch, tmp_path, detached):
    submitted = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "work", "detached": detached})
            error = json.loads(websocket.receive_text())

    assert error["type"] == "error" and error["code"] == "invalid_detached"
    assert submitted == []


@pytest.mark.parametrize("attachment_ids", ["draft-1", {"id": "draft-1"}, [123]])
def test_ws_chat_rejects_invalid_attachment_ids_contract(monkeypatch, tmp_path, attachment_ids):
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "must not be submitted",
                    "attachment_ids": attachment_ids,
                }
            )
            frame = json.loads(websocket.receive_text())

    assert frame["type"] == "error"
    assert "attachment_ids" in frame["error"]
    assert frame["code"] == "invalid_attachment_ids"
    assert frame["message"] == frame["error"]
    assert frame["retryable"] is False


def test_ws_submit_failure_keeps_the_record_for_a_retry(monkeypatch, tmp_path):
    """The record is the transport's outbox journal: nothing compensates a failed submit."""
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )
    monkeypatch.setattr(
        runtime.conversations_repo,
        "delete_message",
        lambda _message_id: pytest.fail("a failed submit must not delete the record"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "attached",
                    "client_message_id": "submit-failed",
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()["messages"]
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["type"] == "error"
    assert error["code"] == "message_submit_failed"
    assert error["retryable"] is True
    assert [message["text"] for message in history] == ["attached"]
    # The attachment stays bound, so the retry reuses it instead of re-uploading.
    assert deleted.status_code == 409

    stored = list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)
    assert [item.metadata.get("inbound_processed") for item in stored] == [False]


def test_the_sweep_resubmits_what_a_failed_submit_left_behind(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "swept", "client_message_id": "sweep-1"})
            assert json.loads(websocket.receive_text())["code"] == "message_submit_failed"

    trace_id = list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)[0].metadata["trace_id"]
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    assert browser_ws.resubmit_unprocessed(runtime.gateway, tmp_path / "data") == 1
    assert [envelope.text for envelope in submitted] == ["swept"]
    assert submitted[0].trace_id == trace_id
    assert [
        item.metadata.get("inbound_processed") for item in list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)
    ] == [True]

    # A processed record is not offered a second time.
    assert browser_ws.resubmit_unprocessed(runtime.gateway, tmp_path / "data") == 0


def test_ws_direct_retry_reuses_the_trace_persisted_by_the_first_attempt(monkeypatch, tmp_path):
    trace_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    trace_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )
    frame = {"type": "chat", "content": "retry me", "client_message_id": "trace-retry"}

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({**frame, "trace_id": trace_a})
            assert json.loads(websocket.receive_text())["code"] == "message_submit_failed"

        submitted = []
        monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({**frame, "trace_id": trace_b})
            echo = json.loads(websocket.receive_text())

    assert echo["trace_id"] == trace_a
    assert [envelope.trace_id for envelope in submitted] == [trace_a]


def test_ws_commit_does_not_take_the_supervisor_queue_lock(monkeypatch, tmp_path):
    """Persist-and-submit spans two systems now; a process-local lock cannot span it."""
    from supervisor.queue import _queue_lock

    lock_owned: list[bool] = []

    def submit(_envelope):
        lock_owned.append(_queue_lock._is_owned())

    _browser_runtime(monkeypatch, tmp_path, submit=submit)
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello"})
            assert json.loads(websocket.receive_text())["role"] == "user"

    assert lock_owned == [False]


def test_ws_retry_reuses_the_attachment_staged_by_the_failed_attempt(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("queue unavailable")),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        frame = {
            "type": "chat",
            "content": "attached",
            "client_message_id": "retry-partial",
            "attachment_ids": [upload["attachment_id"]],
        }
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(frame)
            assert json.loads(websocket.receive_text())["code"] == "message_submit_failed"

        monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: None)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(frame)
            assert json.loads(websocket.receive_text())["role"] == "user"
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert deleted.status_code == 409


def test_ws_rejects_duplicate_attachment_ids_before_binding(monkeypatch, tmp_path):
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "duplicate",
                    "attachment_ids": [upload["attachment_id"], upload["attachment_id"]],
                }
            )
            error = json.loads(websocket.receive_text())
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["type"] == "error"
    assert "unique" in error["error"]
    assert submitted == []
    assert deleted.status_code == 200


@pytest.mark.parametrize("command", ["/status", "/clean"], ids=["owned", "retired"])
def test_ws_rejects_slash_command_with_attachment_atomically(monkeypatch, tmp_path, command):
    """A retired command answers without a run too, so an attachment on one has nowhere to go."""
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": command,
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["type"] == "error"
    assert "command" in error["error"]
    assert error["code"] == "command_with_attachments"
    assert history["messages"] == []
    assert submitted == []
    assert deleted.status_code == 200


@parametrize_from_dict({"chat": {"text": "x" * 11}, "slash argument": {"text": "/stop " + "x" * 10}})
def test_ws_refuses_oversized_text_before_binding_attachments(monkeypatch, tmp_path, text):
    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": text, "attachment_ids": [upload["attachment_id"]]})
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["code"] == "message_too_long"
    assert (
        error["error"]
        == "Сообщение слишком длинное. Максимальная длина — 10 символов. Сократите сообщение и отправьте снова."
    )
    assert history["messages"] == []
    assert submitted == []
    assert deleted.status_code == 200


def test_ws_admits_text_exactly_at_the_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "я" * 10})
            echo = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()

    assert echo["content"] == "я" * 10
    assert submitted[0].text == "я" * 10
    assert history["messages"][0]["text"] == "я" * 10


def test_ws_unprocessed_duplicate_preserves_the_winning_payload(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )
    submitted = []
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "winning payload",
                    "client_message_id": "lost-insert-race",
                    "detached": True,
                }
            )
            first_frames = [json.loads(websocket.receive_text()) for _ in range(2)]
            assert {frame["type"] for frame in first_frames} == {"chat", "error"}
            assert next(frame for frame in first_frames if frame["type"] == "error")["code"] == "message_submit_failed"
            monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "losing payload",
                    "client_message_id": "lost-insert-race",
                    "detached": False,
                }
            )
            conflict = json.loads(websocket.receive_text())

    [winner] = list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)
    assert conflict["type"] == "error" and conflict["code"] == "message_conflict"
    assert submitted == []
    assert winner.content == "winning payload"
    assert winner.metadata["detached"] is True
    assert winner.metadata["inbound_processed"] is False


def test_ws_processed_message_replay_reuses_history_and_task(monkeypatch, tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages

    submitted = []
    _browser_runtime(
        monkeypatch,
        tmp_path,
        submit=lambda envelope: submitted.append(envelope) or SimpleNamespace(intent="chat"),
    )
    frame = {
        "type": "chat",
        "content": "same message",
        "sender_session_id": "tab-1",
        "client_message_id": "stable-1",
        "detached": True,
    }

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(frame)
            first = json.loads(websocket.receive_text())
            websocket.send_json(frame)
            replay = json.loads(websocket.receive_text())
            websocket.send_json({**frame, "detached": False})
            conflict = json.loads(websocket.receive_text())

    messages = list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)
    assert first["type"] == replay["type"] == "chat"
    assert first["client_message_id"] == replay["client_message_id"] == "stable-1"
    assert conflict["type"] == "error" and conflict["code"] == "message_conflict"
    assert len(submitted) == 1
    assert submitted[0].detached is True
    assert len(messages) == 1
    assert messages[0].metadata["inbound_processed"] is True


def test_ws_command_frame_travels_as_an_ordinary_message(monkeypatch, tmp_path):
    """The transport does not recognize commands; the supervisor decides."""
    from ouroboros.storage.chat_store import list_owner_messages

    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "command",
                    "cmd": "/status",
                    "sender_session_id": "tab-1",
                    "client_message_id": "command-1",
                }
            )
            frame = json.loads(websocket.receive_text())

    owner = [
        message for message in list_owner_messages(tmp_path / "data", scope=TEST_SCOPE) if message.actor_type == "owner"
    ]
    assert frame["role"] == "user"
    assert [envelope.text for envelope in submitted] == ["/status"]
    assert [message.content for message in owner] == ["/status"]
    assert owner[0].metadata["inbound_processed"] is True


def test_ws_refuses_a_blocked_operator_command_on_its_own_connection(monkeypatch, tmp_path):
    """A policy refusal is the transport's own answer, so it stays synchronous."""
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)
    monkeypatch.setattr(
        browser_ws,
        "check_operator_command_policy",
        lambda _cmd, _scope: (False, "SAFETY_VIOLATION: blocked"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "rm -rf /"})
            error = json.loads(websocket.receive_text())

    assert error["type"] == "error"
    assert error["code"] == "command_not_allowed"
    assert submitted == []


def test_bound_upload_cannot_be_deleted_by_client_cleanup(monkeypatch, tmp_path):
    _browser_runtime(
        monkeypatch,
        tmp_path,
        submit=lambda _envelope: SimpleNamespace(intent="chat"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "attached",
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            assert json.loads(websocket.receive_text())["role"] == "user"

        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")
        download = client.get(f"/{upload['url']}")

    assert deleted.status_code == 409
    assert download.status_code == 200


def test_ws_outbound_arrives_from_the_outbox(monkeypatch, tmp_path):
    bridge = bootstrap_supervisor_for_tests(tmp_path).bridge

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            bridge.send_message(
                "assistant reply",
                scope=scope_for("browser", browser_ws.BROWSER_CHAT_ID),
                source="browser",
            )
            deadline = time.time() + 2.0
            frame = None
            while time.time() < deadline:
                try:
                    frame = json.loads(websocket.receive_text())
                    if frame.get("type") == "chat" and frame.get("role") == "assistant":
                        break
                except Exception:
                    time.sleep(0.05)
            assert frame is not None
            assert frame["content"] == "assistant reply"
            assert frame["source"] == "browser"


def test_ws_retries_a_delivery_that_never_ran_then_drops_it_loudly(caplog):
    """The ladder is for a delivery the loop never ran — the one thing a retry can fix."""
    hub = browser_ws.BrowserWebSocketHub()
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    hub.clients_for = lambda **_kwargs: [object()]  # type: ignore[method-assign]
    hub.deliver_blocking = lambda _frame, **_kwargs: browser_ws.DeliveryOutcome.NOT_HANDED_OVER  # type: ignore[method-assign]

    deliveries: list[str] = []
    try:
        outbox.publish(
            OutboundMessage(
                scope=scope_for("browser", browser_ws.BROWSER_CHAT_ID), source="browser", text="the answer"
            ),
        )
        with caplog.at_level(logging.ERROR, logger="transport.browser.ws"):
            while (delivered := reader.poll(0.1)) is not None:
                deliveries.append(delivered.message.text)
                hub.handle_outbound(reader, delivered)
    finally:
        reader.close()

    assert deliveries == ["the answer"] * 3
    dropped = [
        record.getMessage()
        for record in caplog.records
        if record.name == "transport.browser.ws" and record.levelno >= logging.ERROR
    ]
    assert len(dropped) == 1
    assert f"chat_id={browser_ws.BROWSER_CHAT_ID}" in dropped[0]
    assert "kind=text" in dropped[0]


def test_ws_acknowledges_a_broadcast_that_reached_a_client():
    hub = browser_ws.BrowserWebSocketHub()
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    hub.clients_for = lambda **_kwargs: [object()]  # type: ignore[method-assign]
    hub.deliver_blocking = lambda _frame, **_kwargs: browser_ws.DeliveryOutcome.DELIVERED  # type: ignore[method-assign]

    try:
        outbox.publish(
            OutboundMessage(scope=scope_for("browser", browser_ws.BROWSER_CHAT_ID), source="browser", text="delivered")
        )
        delivered = reader.poll(0.5)
        assert delivered is not None
        hub.handle_outbound(reader, delivered)
        assert reader.poll(0.05) is None
    finally:
        reader.close()


def test_ws_sweep_supplies_the_journal_an_envelope_builder(monkeypatch, tmp_path):
    """The shared sweep takes an ABC, so a bare function is no longer the contract."""
    runtime = _browser_runtime(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    real = browser_ws.replay_unprocessed

    def _capture(gateway, drive_root, *, source, build, limit):
        captured["build"] = build
        return real(gateway, drive_root, source=source, build=build, limit=limit)

    monkeypatch.setattr(browser_ws, "replay_unprocessed", _capture)

    assert browser_ws.resubmit_unprocessed(runtime.gateway, tmp_path / "data") == 0
    assert isinstance(captured["build"], IEnvelopeBuilder)


def test_ws_heartbeat_smoke(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_ws, "_HEARTBEAT_INTERVAL_SEC", 0.05)
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "heartbeat":
                    assert "ts" in frame
                    return
            pytest.fail("expected heartbeat frame")


def test_ws_reconnect_after_disconnect(monkeypatch, tmp_path):
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    monkeypatch.setattr(runtime.gateway.inbox, "submit", lambda _envelope: SimpleNamespace(intent="chat"))

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "first"})
            assert json.loads(websocket.receive_text())["content"] == "first"

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "after reconnect"})
            frame = json.loads(websocket.receive_text())
            assert frame["type"] == "chat"
            assert frame["role"] == "user"
            assert frame["content"] == "after reconnect"


def test_ws_log_and_extension_lifecycle_frames(monkeypatch, tmp_path):
    bootstrap_supervisor_for_tests(tmp_path)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            from ouroboros.utils import emit_ui_log

            emit_ui_log({"level": "info", "message": "worker started"})
            from ouroboros.observability.event_bus import SKILL_LIFECYCLE, publish_event

            publish_event(SKILL_LIFECYCLE, {"skill": "demo", "action": "load", "status": "ok"})
            seen: set[str] = set()
            deadline = time.time() + 2.0
            while time.time() < deadline and len(seen) < 2:
                frame = json.loads(websocket.receive_text())
                seen.add(str(frame.get("type")))
            assert "log" in seen
            assert "extension_lifecycle" in seen


def test_ws_task_done_runtime_event_arrives_as_log(monkeypatch, tmp_path):
    bootstrap_supervisor_for_tests(tmp_path)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            from supervisor.events import _handle_task_done

            _handle_task_done(
                TaskFinished(
                    scope=TEST_SCOPE,
                    task_id="done-1",
                    task_type="task",
                    total_rounds=2,
                    prompt_tokens=3,
                    completion_tokens=4,
                    ts="2026-01-01T00:00:01+00:00",
                ),
                SimpleNamespace(
                    DRIVE_ROOT=tmp_path,
                    state_repo=SimpleNamespace(audit_event_repository=sql_audit_event_repository()),
                ),
            )

            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "log" and frame.get("data", {}).get("type") == "task_done":
                    assert frame["data"]["task_id"] == "done-1"
                    return
            pytest.fail("expected task_done log frame")


def test_browser_ws_hub_stop_releases_the_reader_and_event_subscription():
    from ouroboros.observability.event_bus import init_global_event_bus
    from transport.browser.ws import BrowserWebSocketHub
    from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox

    async def scenario():
        bus = init_global_event_bus()
        gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
        hub = BrowserWebSocketHub(heartbeat_interval_sec=3600)

        await hub.start(asyncio.get_running_loop(), gateway=gateway)
        assert hub._reader is not None
        assert any(entry["skill_name"] == "browser-ingress" for entry in bus.snapshot().values())

        await hub.stop()
        assert hub._reader is None
        assert not any(entry["skill_name"] == "browser-ingress" for entry in bus.snapshot().values())

    asyncio.run(scenario())


def test_browser_ws_hub_opens_its_subscription_off_the_event_loop():
    from transport.browser.ws import BrowserWebSocketHub
    from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox

    opened_on: list[int] = []

    class _BlockingOutbox(InProcessOutbox):
        def reader(self, *, name, mode):
            opened_on.append(threading.get_ident())
            time.sleep(0.05)
            return super().reader(name=name, mode=mode)

    async def scenario():
        gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=_BlockingOutbox())
        hub = BrowserWebSocketHub(heartbeat_interval_sec=3600)
        try:
            await hub.start(asyncio.get_running_loop(), gateway=gateway)
        finally:
            await hub.stop()
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())

    assert opened_on and loop_thread not in opened_on


# -- one owner's stalled socket is not every owner's pause ----------------------


class _FakeSocket:
    """A socket that takes frames, or one that never does."""

    def __init__(self, *, stalls: bool = False) -> None:
        self.sent: list[str] = []
        self.received_at: list[float] = []
        self.closed = False
        self.close_seen = asyncio.Event()
        self._stalls = stalls

    async def send_text(self, data: str) -> None:
        if self._stalls:
            await asyncio.sleep(3600)
        self.received_at.append(time.monotonic())
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_seen.set()


def _client(hub, *, owner_id: str, chat_id: str = "1", agent_id: str = "main", stalls: bool = False):
    from new_core.identity import OwnerScope

    socket = _FakeSocket(stalls=stalls)
    client = browser_ws.BrowserClient(
        websocket=socket,  # type: ignore[arg-type]
        scope=OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id="browser", chat_id=chat_id),
    )
    hub.add_client(client)
    return client, socket


def test_a_stalled_tab_does_not_delay_the_owners_other_tab(monkeypatch):
    """Both connections are in one fan-out, so a serial loop pays the deadline first.

    This is the case the change exists for: a per-connection deadline applied in
    sequence is not a bound, it is the roster's size expressed in seconds.
    """
    monkeypatch.setattr(browser_ws, "_CLIENT_SEND_TIMEOUT_SEC", 1.0)
    hub = browser_ws.BrowserWebSocketHub()

    async def _run() -> tuple[float, _FakeSocket]:
        # The stalled tab is registered first, so a serial loop reaches it first.
        _client(hub, owner_id="alice", stalls=True)
        _, live = _client(hub, owner_id="alice")
        started = time.monotonic()
        # Bounded, so a fan-out with no deadline at all fails instead of hanging.
        await asyncio.wait_for(
            hub.deliver({"type": "chat", "content": "hello"}, owner_id="alice", agent_id="main", chat_id="1"), 5.0
        )
        return started, live

    started, live = asyncio.run(_run())

    assert len(live.sent) == 1, "the live tab must receive the frame"
    # What must be fast is the live tab's receipt. `deliver` itself returns only
    # once every connection is decided, which is one deadline, not one per tab.
    assert live.received_at[0] - started < 0.5, "the live tab waited out the stalled tab's deadline"


def test_a_stalled_connection_does_not_delay_the_other_owners(monkeypatch):
    """The bound is one deadline, not one per connection — that is the whole change."""
    monkeypatch.setattr(browser_ws, "_CLIENT_SEND_TIMEOUT_SEC", 0.2)
    hub = browser_ws.BrowserWebSocketHub()

    async def _run() -> tuple[float, list[str]]:
        _client(hub, owner_id="alice", stalls=True)
        _, live = _client(hub, owner_id="bob")
        started = time.monotonic()
        # Bounded, so a serial fan-out fails the case instead of hanging it.
        await asyncio.wait_for(
            hub.deliver({"type": "chat", "content": "for alice"}, owner_id="alice", agent_id="main", chat_id="1"), 3.0
        )
        await asyncio.wait_for(
            hub.deliver({"type": "chat", "content": "for bob"}, owner_id="bob", agent_id="main", chat_id="1"), 3.0
        )
        return time.monotonic() - started, live.sent

    elapsed, delivered = asyncio.run(_run())

    assert len(delivered) == 1, "the live owner's frame must arrive"
    assert elapsed < 1.0, "bob waited out alice's socket"


def test_a_connection_that_never_takes_its_frame_is_closed_and_dropped(monkeypatch):
    monkeypatch.setattr(browser_ws, "_CLIENT_SEND_TIMEOUT_SEC", 0.2)
    hub = browser_ws.BrowserWebSocketHub()

    async def _run():
        _, socket = _client(hub, owner_id="alice", stalls=True)
        taken = await hub.deliver(
            {"type": "chat", "content": "never taken"}, owner_id="alice", agent_id="main", chat_id="1"
        )
        # The close runs as its own task, and it first waits out — then cancels —
        # the send it must not write past. Waited on rather than slept past.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(socket.close_seen.wait(), 3.0)
        return taken, socket

    taken, socket = asyncio.run(_run())

    assert taken == 0
    assert hub.clients_for(owner_id="alice", agent_id="main", chat_id="1") == []
    assert socket.closed, "a socket that stopped draining must be closed, not left half-alive"


def test_a_delivery_that_reached_nobody_is_acknowledged(monkeypatch):
    """A stalled connection is not a transient failure, so it must not drive the ladder."""
    hub = browser_ws.BrowserWebSocketHub()
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    hub.clients_for = lambda **_kwargs: [object()]  # type: ignore[method-assign]
    hub.deliver_blocking = lambda _frame, **_kwargs: browser_ws.DeliveryOutcome.NO_RECIPIENT  # type: ignore[method-assign]

    try:
        outbox.publish(
            OutboundMessage(scope=scope_for("browser", browser_ws.BROWSER_CHAT_ID), source="browser", text="gone")
        )
        delivered = reader.poll(0.5)
        assert delivered is not None

        hub.handle_outbound(reader, delivered)
        assert reader.poll(0.05) is None, "acknowledged, not offered again"
    finally:
        reader.close()


def test_a_delivery_that_started_and_overran_is_acknowledged(caplog):
    """A blocked loop is not a failed handover; retrying spends the next message's budget."""
    hub = browser_ws.BrowserWebSocketHub()
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    hub.clients_for = lambda **_kwargs: [object()]  # type: ignore[method-assign]
    hub.deliver_blocking = lambda _frame, **_kwargs: browser_ws.DeliveryOutcome.FAILED  # type: ignore[method-assign]

    try:
        outbox.publish(
            OutboundMessage(scope=scope_for("browser", browser_ws.BROWSER_CHAT_ID), source="browser", text="slow")
        )
        delivered = reader.poll(0.5)
        assert delivered is not None

        hub.handle_outbound(reader, delivered)
        assert reader.poll(0.05) is None
    finally:
        reader.close()


@pytest.mark.parametrize("oversized", [False, True], ids=["missing-reference", "lowered-limit"])
def test_unresolvable_photo_is_dropped_before_next_message(isolate_settings, monkeypatch, tmp_path, oversized):
    from sqlalchemy import text

    from tests.helpers.files import stored_file
    from transport.interfaces import AttachmentRef

    runtime = _browser_runtime(monkeypatch, tmp_path)
    reference = None
    if oversized:
        metadata = stored_file(data=b"RIFF1234WEBP", mime="image/webp", filename="image.webp")
        with runtime_persistence.current.engine.begin() as conn:
            conn.execute(text("UPDATE files SET size=1 WHERE id=:id"), {"id": metadata["id"]})
        monkeypatch.setenv("OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES", "4")
        reference = AttachmentRef(metadata["id"], "image.webp", "image/webp", 1)
    with recording_blob_double() as recorder, _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            runtime.gateway.outbox.publish(
                OutboundMessage(scope=TEST_SCOPE, source="browser", kind=OutboundKind.PHOTO, attachment=reference)
            )
            runtime.gateway.outbox.publish(
                OutboundMessage(scope=TEST_SCOPE, source="browser", text="after rejected photo")
            )
            assert websocket.receive_json()["content"] == "after rejected photo"
        assert recorder.unbounded_reads == []


def test_a_resolution_that_overruns_its_deadline_drops_the_message(monkeypatch, tmp_path):
    from tests.helpers.files import BlockedFileDownload, stored_file
    from transport.interfaces import AttachmentRef

    monkeypatch.setattr(browser_ws, "_PAYLOAD_RESOLVE_TIMEOUT_SEC", 0.2)
    runtime = _browser_runtime(monkeypatch, tmp_path)
    metadata = stored_file(data=b"RIFF1234WEBP", mime="image/webp", filename="image.webp")
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        browser_ws.get_browser_ws_hub().file_blob = BlockedFileDownload()
        with client.websocket_connect("/ws") as websocket:
            started = time.monotonic()
            runtime.gateway.outbox.publish(
                OutboundMessage(
                    scope=TEST_SCOPE,
                    source="browser",
                    kind=OutboundKind.PHOTO,
                    attachment=AttachmentRef(metadata["id"], "image.webp", "image/webp", metadata["size"]),
                )
            )
            runtime.gateway.outbox.publish(OutboundMessage(scope=TEST_SCOPE, source="browser", text="after slow photo"))
            assert websocket.receive_json()["content"] == "after slow photo"
            assert time.monotonic() - started < 2


def test_frames_for_one_conversation_leave_in_the_order_they_were_published():
    hub = browser_ws.BrowserWebSocketHub()

    async def _run() -> list[str]:
        _, socket = _client(hub, owner_id="alice")
        for index in range(5):
            await hub.deliver({"type": "chat", "content": f"m{index}"}, owner_id="alice", agent_id="main", chat_id="1")
        return [json.loads(frame)["content"] for frame in socket.sent]

    assert asyncio.run(_run()) == [f"m{index}" for index in range(5)]


def test_an_error_frame_does_not_interleave_with_a_delivery():
    """The lock belongs to the connection, so the writers that never see the pairing take it too."""

    class _Counting(_FakeSocket):
        def __init__(self) -> None:
            super().__init__()
            self.inside = 0
            self.overlapped = False

        async def send_text(self, data: str) -> None:
            self.inside += 1
            self.overlapped = self.overlapped or self.inside > 1
            await asyncio.sleep(0)
            self.inside -= 1
            self.sent.append(data)

    hub = browser_ws.BrowserWebSocketHub()

    async def _run() -> _Counting:
        from new_core.identity import OwnerScope

        socket = _Counting()
        hub.add_client(
            browser_ws.BrowserClient(
                websocket=socket,  # type: ignore[arg-type]
                scope=OwnerScope(owner_id="alice", agent_id="main", surface_id="browser", chat_id="1"),
            )
        )
        await asyncio.gather(
            hub.deliver({"type": "chat", "content": "outbound"}, owner_id="alice", agent_id="main", chat_id="1"),
            browser_ws._send_ws_error(socket, "nope", code="invalid_json"),  # type: ignore[arg-type]
        )
        return socket

    socket = asyncio.run(_run())

    assert not socket.overlapped
    assert len(socket.sent) == 2


def test_two_frames_never_interleave_on_one_connection():
    """Two coroutines inside one ASGI send corrupt the frame stream, not just its order."""
    hub = browser_ws.BrowserWebSocketHub()

    class _Interleaving(_FakeSocket):
        def __init__(self) -> None:
            super().__init__()
            self.inside = 0
            self.overlapped = False

        async def send_text(self, data: str) -> None:
            self.inside += 1
            self.overlapped = self.overlapped or self.inside > 1
            await asyncio.sleep(0)
            self.inside -= 1
            self.sent.append(data)

    async def _run() -> _Interleaving:
        from new_core.identity import OwnerScope

        socket = _Interleaving()
        hub.add_client(
            browser_ws.BrowserClient(
                websocket=socket,  # type: ignore[arg-type]
                scope=OwnerScope(owner_id="alice", agent_id="main", surface_id="browser", chat_id="1"),
            )
        )
        await asyncio.gather(
            hub.deliver({"type": "chat", "content": "outbound"}, owner_id="alice", agent_id="main", chat_id="1"),
            hub.broadcast({"type": "heartbeat"}),
        )
        return socket

    socket = asyncio.run(_run())

    assert not socket.overlapped, "one connection must have a single writer at a time"
    assert len(socket.sent) == 2


def test_the_join_timeout_covers_the_longest_a_message_can_hold_the_slot():
    """A shorter join releases the subscription under a reader still holding a message."""
    assert browser_ws._MESSAGE_BUDGET_SEC == (
        browser_ws._PAYLOAD_RESOLVE_TIMEOUT_SEC + browser_ws._DELIVERY_TIMEOUT_SEC
    )
    assert browser_ws._READER_JOIN_TIMEOUT_SEC > browser_ws._OUTBOX_POLL_TIMEOUT_SEC + browser_ws._MESSAGE_BUDGET_SEC


# -- the echo of a user's own line is not a process-local send ------------------


def _delivering(hub, frames: list[tuple[str, dict]]):
    """Stand in for the socket write, keeping the hub's real recipient selection."""

    def _deliver(frame, *, owner_id, agent_id, chat_id):
        recipients = hub.clients_for(owner_id=owner_id, agent_id=agent_id, chat_id=chat_id)
        for client in recipients:
            frames.append((client.scope.owner_id, frame))
        return browser_ws.DeliveryOutcome.DELIVERED if recipients else browser_ws.DeliveryOutcome.NO_RECIPIENT

    return _deliver


def _instance(outbox, frames):
    """One transport instance: its own hub and its own BROADCAST subscription."""
    hub = browser_ws.BrowserWebSocketHub()
    hub.deliver_blocking = _delivering(hub, frames)  # type: ignore[method-assign]
    return hub, outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)


def _connect(hub, owner_id: str, chat_id: str = "1"):
    from new_core.identity import OwnerScope

    client = browser_ws.BrowserClient(
        websocket=object(),  # type: ignore[arg-type]
        scope=OwnerScope(owner_id=owner_id, agent_id="main", surface_id="browser", chat_id=chat_id),
    )
    hub.add_client(client)
    return client


def test_the_echo_reaches_another_instance_only_within_the_conversation(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    frames = []
    hub, reader = _instance(runtime.gateway.outbox, frames)
    _connect(hub, TEST_SCOPE.owner_id)
    _connect(hub, "another-owner")
    _connect(hub, TEST_SCOPE.owner_id, chat_id="9")
    try:
        with _browser_ws_client(monkeypatch, tmp_path) as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json(
                    {"type": "chat", "content": "hello", "sender_session_id": "tab-1", "client_message_id": "cmid-1"}
                )
                first = websocket.receive_json()
                delivered = reader.poll(2)
                assert delivered is not None
                hub.handle_outbound(reader, delivered)
        assert len(frames) == 1 and frames[0][0] == TEST_SCOPE.owner_id
        second = frames[0][1]
        assert second == first
        assert second["role"] == "user" and second["content"] == "hello"
        assert second["sender_session_id"] == "tab-1" and second["client_message_id"] == "cmid-1"
    finally:
        reader.close()


def test_an_echo_the_outbox_refuses_is_reported_rather_than_swallowed(monkeypatch, tmp_path):
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()
    gateway.outbox.failure = RuntimeError("outbox is unavailable")
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        client.app.state.message_gateway = gateway
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello"})
            assert websocket.receive_json()["code"] == "message_echo_failed"
        assert client.get("/api/chat/history").json()["messages"][0]["text"] == "hello"
    assert [message.text for message in gateway.inbox.submitted] == ["hello"]


def test_the_echo_names_the_time_the_row_was_stored(monkeypatch, tmp_path):
    """Not the locally minted one: a lost insert race leaves the row carrying another time.

    Two echoes of one message — the first and the replay of an already-processed row —
    must not name two different times, and only the stored one is the same on both.
    """
    from ouroboros.storage.chat_store import list_owner_messages

    _browser_runtime(monkeypatch, tmp_path, submit=lambda _envelope: None)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello", "client_message_id": "cmid-1"})
            deadline = time.time() + 3.0
            echo = None
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "chat" and frame.get("role") == "user":
                    echo = frame
                    break

    assert echo is not None, "the echo never arrived"
    stored = [
        message for message in list_owner_messages(tmp_path / "data", scope=TEST_SCOPE) if message.actor_type == "owner"
    ]
    assert echo["ts"] == stored[0].created_at


def test_an_answer_produced_on_the_submit_arrives_after_the_echo(monkeypatch, tmp_path):
    """Both travel one subscription now, and a slash command is answered on the submit.

    Publishing the echo afterwards would render the answer above the line it answers —
    which the inline send used to make impossible.
    """
    runtime = _browser_runtime(monkeypatch, tmp_path)
    bridge = runtime.bridge

    def _answer_on_submit(envelope):
        bridge.send_message("the answer", scope=envelope.scope, source="browser")

    monkeypatch.setattr(runtime.gateway.inbox, "submit", _answer_on_submit)

    order: list[str] = []
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "/status", "client_message_id": "cmid-1"})
            deadline = time.time() + 3.0
            while time.time() < deadline and len(order) < 2:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "chat" and frame.get("role") in ("user", "assistant"):
                    order.append(frame["role"])

    assert order == ["user", "assistant"], "the answer arrived above the line it answers"


def test_the_zero_conversation_is_matched_rather_than_read_as_absent():
    """A chat of ``"0"`` is a conversation the boundary admits, and the seam narrows it to ``0``.

    Falsy, so a ``chat_id or ""`` guard turned it into the key no connection holds: the
    handshake succeeded, history worked, and every answer was acknowledged and dropped with
    no log line.
    """
    from transport.interfaces import OutboundMessage, routed_chat_id

    hub = browser_ws.BrowserWebSocketHub()
    client, _socket = _client(hub, owner_id="alice", agent_id="main", chat_id="0")
    message = OutboundMessage(scope=client.scope, source="browser", text="the answer")

    assert routed_chat_id(client.scope) == 0
    assert hub.clients_for(owner_id="alice", agent_id="main", chat_id=message.chat_id) == [client]
    assert hub.clients_for(owner_id="alice", agent_id="main", chat_id="0") == [client]
    # And a chat nobody holds is still no connection at all.
    assert hub.clients_for(owner_id="alice", agent_id="main", chat_id=None) == []


def test_one_agents_answer_does_not_reach_another_agents_socket():
    """A chat id is unique within an owner and an agent, not across an owner's agents."""
    from transport.interfaces import OutboundMessage

    hub = browser_ws.BrowserWebSocketHub()
    mine, _ = _client(hub, owner_id="alice", agent_id="main", chat_id="7")
    theirs, _ = _client(hub, owner_id="alice", agent_id="analyst", chat_id="7")
    message = OutboundMessage(scope=theirs.scope, source="browser", text="the analyst answered")

    matched = hub.clients_for(owner_id="alice", agent_id=message.scope.agent_id, chat_id=message.chat_id)

    assert matched == [theirs]
    assert mine not in matched


# -- the conversation record the first message creates ---------------------------


def _recorded(tmp_path, chat_id: str = "1"):
    from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryStore
    from tests.helpers.shared import TEST_SCOPE, scope_with

    return SqlChatRegistryStore().get_chat(scope_with(TEST_SCOPE, chat_id=chat_id))


def test_a_conversations_first_message_records_it_with_a_provisional_title(monkeypatch, tmp_path):
    """The row is born on the first message, so a list renders before any model has run."""
    _browser_runtime(monkeypatch, tmp_path)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "  сколько   стоит   отпуск  \nвторая строка"})
            json.loads(websocket.receive_text())

    recorded = _recorded(tmp_path)
    assert recorded is not None
    # First line only, interior whitespace collapsed, no stored ellipsis.
    assert recorded.title == "сколько стоит отпуск"
    assert recorded.title_source.value == "provisional"
    assert recorded.is_active is True


def test_a_message_on_a_retired_conversation_is_refused_and_persists_nothing(monkeypatch, tmp_path):
    """A refusal, never a revival: a stale tab must not resurrect a conversation the owner ended."""
    from sqlalchemy import text as sql

    from ouroboros.storage.chat_store import list_owner_messages
    from tests.helpers.shared import TEST_SCOPE

    _browser_runtime(monkeypatch, tmp_path)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "first"})
            json.loads(websocket.receive_text())
            with runtime_persistence.current.engine.begin() as conn:
                conn.execute(sql("UPDATE chats SET is_active = false"))
            websocket.send_json({"type": "chat", "content": "second"})
            refusal = json.loads(websocket.receive_text())

    assert refusal["type"] == "error"
    assert refusal["code"] == "chat_retired"
    assert [row.content for row in list_owner_messages(tmp_path / "data", scope=TEST_SCOPE)] == ["first"]


def test_a_registry_that_raises_refuses_the_message_rather_than_persisting_it(monkeypatch, tmp_path):
    """The dependency is hard: no record, no message — and the client is told to retry."""
    from ouroboros.storage.chat_store import list_owner_messages
    from tests.helpers.shared import TEST_SCOPE

    _browser_runtime(monkeypatch, tmp_path)

    def refuse(self, connection, scope, *, provisional_title):
        raise RuntimeError("the registry is unreachable")

    monkeypatch.setattr(
        "config_registry.chat_registry.infra.repositories.repo.SqlChatRegistryStore.ensure_chat_in_transaction", refuse
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "не дойдёт"})
            refusal = json.loads(websocket.receive_text())

    assert refusal["type"] == "error"
    assert (refusal["code"], refusal["retryable"]) == ("message_persistence_failed", True)
    assert list_owner_messages(tmp_path / "data", scope=TEST_SCOPE) == []


def test_a_retirement_leaves_alone_the_uploads_an_existing_row_still_names(monkeypatch, tmp_path):
    """Release only what this call bound: the sweep still owes the row its uploads."""
    from sqlalchemy import text as sql

    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        message = {
            "type": "chat",
            "content": "с вложением",
            "client_message_id": "held-1",
            "attachment_ids": [upload["attachment_id"]],
        }
        with client.websocket_connect("/ws") as websocket:
            # Persisted but unprocessed, so the retry falls through to the commit rather than
            # replaying the echo — the one path where the retired branch meets an existing row.
            websocket.send_json(message)
            assert json.loads(websocket.receive_text())["code"] == "message_submit_failed"
            with runtime_persistence.current.engine.begin() as conn:
                conn.execute(sql("UPDATE chats SET is_active = false"))
            websocket.send_json(message)
            # The first message's echo travels the outbox and is still pending: it went out
            # before the submit that failed, so the refusal is the next *error* frame.
            refusal = json.loads(websocket.receive_text())
            while refusal.get("type") != "error":
                refusal = json.loads(websocket.receive_text())

    assert refusal["code"] == "chat_retired"
    from ouroboros.storage.chat_attachments import get_attachment

    assert get_attachment(TEST_SCOPE, upload["attachment_id"])["status"] == "ready"


def test_a_dialogs_message_writes_no_conversation_record(monkeypatch, tmp_path):
    """The registry is the browser's; dialogs and A2A keep minting the ids they mint today."""
    from ouroboros.storage.chat_store import append_chat_message
    from tests.helpers.scope import scope_for

    runtime_persistence.initialize()
    scope = scope_for("dialogs", "peer-42")
    append_chat_message(
        tmp_path / "data",
        scope=scope,
        actor_type="owner",
        chat_id="peer-42",
        user_id=1,
        text="привет",
        source="dialogs",
    )

    assert _recorded(tmp_path, "peer-42") is None


def test_ws_refuses_oversized_command_before_policy_check(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    monkeypatch.setattr(browser_ws, "_POLICY_CHECK_SLOTS", threading.BoundedSemaphore(0))
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "x" * 11})
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()

    assert error["code"] == "message_too_long"
    assert "Максимальная длина — 10 символов" in error["error"]
    assert history["messages"] == []
    assert submitted == []
