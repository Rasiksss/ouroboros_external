from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ouroboros.gateway import browser_ws
from ouroboros.gateway.contracts import WS_MESSAGE_TYPES
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.chat_attachments import cleanup_expired_attachments
from supervisor.message_bus import LocalChatBridge
from supervisor.testing import bootstrap_supervisor_for_tests
from tests.helpers.server_client import patched_server_client

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
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data", bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)
    if submit is not None:
        monkeypatch.setattr(runtime.orchestrator, "submit", submit)
    return runtime


def _upload_text(client):
    return client.post(
        "/api/chat/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
    ).json()


def test_ws_chat_smoke_without_orchestrator(monkeypatch, tmp_path):
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello"})
            init_frame = json.loads(websocket.receive_text())
            assert init_frame["type"] == "chat"
            assert init_frame["role"] == "assistant"
            assert "initializing" in init_frame["content"].lower()
        assert client.get("/api/chat/history").json()["messages"] == []


def test_ws_plain_text_does_not_require_attachment_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        browser_ws,
        "create_storage_provider",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "plain text"})
            frame = json.loads(websocket.receive_text())

    assert frame["type"] == "chat"
    assert frame["role"] == "assistant"
    assert "initializing" in frame["content"].lower()


def test_ws_chat_submits_to_orchestrator(monkeypatch, tmp_path):
    submitted: list[dict] = []
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    def _capture_submit(envelope):
        submitted.append(
            {
                "text": envelope.effective_text(),
                "source": envelope.source,
                "chat_id": envelope.chat_id,
            }
        )
        from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
        from supervisor.orchestrator.orchestrator import SubmitResult

        return SubmitResult(intent=TaskIntentKind.AGENT_CHAT, mode=SubmissionMode.QUEUE, task_id="t1")

    monkeypatch.setattr(runtime.orchestrator, "submit", _capture_submit)

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
                    "chat_id": browser_ws.BROWSER_CHAT_ID,
                }
            ]
            assert runtime.state_repo.load().last_owner_message_at


def test_ws_chat_submits_when_runtime_state_is_unavailable(monkeypatch, tmp_path):
    submitted: list[str] = []
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    def _load_failed():
        raise OSError("state unavailable")

    def _capture_submit(envelope):
        submitted.append(envelope.effective_text())

    monkeypatch.setattr(runtime.state_repo, "load", _load_failed)
    monkeypatch.setattr(runtime.orchestrator, "submit", _capture_submit)

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
    assert replay["type"] == "error"
    assert "attachment not found" in replay["error"]
    assert submitted == [
        {
            "text": "",
            "attachment_ids": [upload["attachment_id"]],
            "image_base64": "",
        }
    ]


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


def test_ws_bound_attachment_expires_via_regular_ttl_cleanup(monkeypatch, tmp_path):
    import ouroboros.storage.chat_attachments as chat_attachments

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

        expired_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(chat_attachments, "_now", lambda: expired_now)
        response = client.get(f"/{upload['url']}")
        full_path_response = client.get(
            f"/api/chat/files/chat/{upload['attachment_id']}/note.txt",
        )
        cleanup_expired_attachments(create_storage_provider(data_dir=tmp_path / "data"), now=expired_now)
        history = client.get("/api/chat/history").json()

    assert response.status_code == 404
    assert full_path_response.status_code == 404
    attachment = history["messages"][0]["attachments"][0]
    assert attachment["status"] == "expired"
    assert "url" not in attachment


def test_ws_bind_failure_does_not_persist_message(monkeypatch, tmp_path):
    _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_ws,
        "bind_attachments",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("binding failed")),
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
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()

    assert error["type"] == "error"
    assert history["messages"] == []


def test_ws_submit_failure_rolls_back_message_and_attachment_binding(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.orchestrator,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("task queue unavailable")),
    )
    order: list[str] = []
    original_release = browser_ws.release_attachments
    original_delete = runtime.store.delete_message

    def release_first(*args, **kwargs):
        order.append("release")
        return original_release(*args, **kwargs)

    def delete_second(message_id):
        order.append("delete")
        return original_delete(message_id)

    monkeypatch.setattr(browser_ws, "release_attachments", release_first)
    monkeypatch.setattr(runtime.store, "delete_message", delete_second)

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
        history = client.get("/api/chat/history").json()
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["type"] == "error"
    assert "submit" in error["error"]
    assert history["messages"] == []
    assert deleted.status_code == 200
    assert order == ["release", "delete"]


def test_ws_persistence_and_submit_share_clean_queue_lock(monkeypatch, tmp_path):
    from supervisor.queue import _queue_lock

    lock_owned: list[bool] = []

    def submit(_envelope):
        lock_owned.append(_queue_lock._is_owned())
        return SimpleNamespace(intent="chat")

    _browser_runtime(monkeypatch, tmp_path, submit=submit)
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello"})
            assert json.loads(websocket.receive_text())["role"] == "user"

    assert lock_owned == [True]


def test_ws_unknown_submit_commit_keeps_message_for_retry(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)

    class UnknownCommit(RuntimeError):
        pass

    monkeypatch.setattr(browser_ws, "SubmissionCommitUnknown", UnknownCommit, raising=False)
    monkeypatch.setattr(
        runtime.orchestrator,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(UnknownCommit("unknown")),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "attached",
                    "client_message_id": "submit-unknown",
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            error = json.loads(websocket.receive_text())
        history = client.get("/api/chat/history").json()["messages"]
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")

    assert error["code"] == "message_submit_unknown"
    assert error["retryable"] is True
    assert [message["text"] for message in history] == ["attached"]
    assert deleted.status_code == 409


def test_ws_retry_rebinds_attachment_after_partial_rollback(monkeypatch, tmp_path):
    runtime = _browser_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime.orchestrator,
        "submit",
        lambda _envelope: (_ for _ in ()).throw(OSError("queue unavailable")),
    )
    monkeypatch.setattr(runtime.store, "delete_message", lambda _message_id: False)

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

        monkeypatch.setattr(
            runtime.orchestrator,
            "submit",
            lambda _envelope: SimpleNamespace(intent="chat"),
        )
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


def test_ws_rejects_slash_command_with_attachment_atomically(monkeypatch, tmp_path):
    submitted: list = []
    _browser_runtime(monkeypatch, tmp_path, submit=submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "/status",
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


def test_ws_duplicate_client_message_id_releases_new_attachment(monkeypatch, tmp_path):
    from ouroboros.gateway import browser_ws

    _browser_runtime(
        monkeypatch,
        tmp_path,
        submit=lambda _envelope: SimpleNamespace(intent="chat"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        upload = _upload_text(client)
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "first", "client_message_id": "duplicate-1"})
            assert json.loads(websocket.receive_text())["role"] == "user"
            original_get = browser_ws.get_chat_message_by_client_id
            calls = 0

            def raced_get(*args, **kwargs):
                nonlocal calls
                calls += 1
                return None if calls == 1 else original_get(*args, **kwargs)

            monkeypatch.setattr(browser_ws, "get_chat_message_by_client_id", raced_get)
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "duplicate",
                    "client_message_id": "duplicate-1",
                    "attachment_ids": [upload["attachment_id"]],
                }
            )
            duplicate = json.loads(websocket.receive_text())
        deleted = client.delete(f"/api/chat/upload?id={upload['attachment_id']}")
        history = client.get("/api/chat/history").json()

    assert duplicate["type"] == "error"
    assert "duplicate" in duplicate["error"]
    assert deleted.status_code == 200
    assert [message["text"] for message in history["messages"]] == ["first"]


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
    }

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(frame)
            first = json.loads(websocket.receive_text())
            websocket.send_json(frame)
            replay = json.loads(websocket.receive_text())

    messages = list_owner_messages(tmp_path / "data")
    assert first["type"] == replay["type"] == "chat"
    assert first["client_message_id"] == replay["client_message_id"] == "stable-1"
    assert len(submitted) == 1
    assert len(messages) == 1
    assert messages[0].metadata["inbound_processed"] is True


def test_ws_command_frame_persists_processed_receipt(monkeypatch, tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages

    _browser_runtime(monkeypatch, tmp_path)

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
            response = json.loads(websocket.receive_text())

    owner = [message for message in list_owner_messages(tmp_path / "data") if message.actor_type == "owner"]
    assert response["role"] == "assistant"
    assert [message.content for message in owner] == ["/status"]
    assert owner[0].metadata["inbound_processed"] is True


def test_ws_clean_uses_replay_safe_history_path(monkeypatch, tmp_path):
    from ouroboros.storage.chat_store import append_chat_message

    _browser_runtime(monkeypatch, tmp_path)
    append_chat_message(
        tmp_path / "data",
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="old",
        source="browser",
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json(
                {
                    "type": "chat",
                    "content": "/clean",
                    "sender_session_id": "tab-1",
                    "client_message_id": "clean-1",
                }
            )
            frames = [json.loads(websocket.receive_text()), json.loads(websocket.receive_text())]
        history = client.get("/api/chat/history").json()["messages"]

    assert {frame["type"] for frame in frames} == {"chat", "chat_history_cleared"}
    assert [message["text"] for message in history] == ["/clean", "История очищена"]


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


def test_ws_outbound_via_bridge_broadcast(monkeypatch, tmp_path):
    bridge = LocalChatBridge({})
    bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            bridge.send_message(
                browser_ws.BROWSER_CHAT_ID,
                "assistant reply",
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
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)
    monkeypatch.setattr(runtime.orchestrator, "submit", lambda _envelope: SimpleNamespace(intent="chat"))

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
    bridge = LocalChatBridge({})
    bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            bridge.push_log({"level": "info", "message": "worker started"})
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
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            from supervisor.events import _handle_task_done

            _handle_task_done(
                {
                    "type": "task_done",
                    "task_id": "done-1",
                    "task_type": "task",
                    "total_rounds": 2,
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "ts": "2026-01-01T00:00:01+00:00",
                },
                SimpleNamespace(DRIVE_ROOT=tmp_path, executor=runtime.executor),
            )

            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "log" and frame.get("data", {}).get("type") == "task_done":
                    assert frame["data"]["task_id"] == "done-1"
                    return
            pytest.fail("expected task_done log frame")


def test_browser_ws_hub_stop_releases_bridge_and_event_subscription():
    from ouroboros.gateway.browser_ws import BrowserWebSocketHub
    from ouroboros.observability.event_bus import init_global_event_bus

    class Bridge:
        def __init__(self):
            self.sink = None

        def set_browser_sink(self, sink):
            self.sink = sink

        def push_log(self, _event):
            return None

    async def scenario():
        bus = init_global_event_bus()
        bridge = Bridge()
        hub = BrowserWebSocketHub(heartbeat_interval_sec=3600)

        await hub.start(asyncio.get_running_loop(), bridge=bridge)
        assert bridge.sink is not None
        assert any(entry["skill_name"] == "browser-ingress" for entry in bus.snapshot().values())

        await hub.stop()
        assert bridge.sink is None
        assert not any(entry["skill_name"] == "browser-ingress" for entry in bus.snapshot().values())

    asyncio.run(scenario())
