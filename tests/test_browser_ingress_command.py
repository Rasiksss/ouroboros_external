from __future__ import annotations

import json
import time

import pytest

from ouroboros.gateway import browser_ws
from supervisor.message_bus import LocalChatBridge
from supervisor.runtime_context import reset_runtime_for_tests
from supervisor.testing import bootstrap_supervisor_for_tests
from tests.helpers.server_client import patched_server_client


def _browser_ws_client(monkeypatch, tmp_path):
    return patched_server_client(
        monkeypatch,
        tmp_path,
        browser_enabled=True,
        reset_browser_ws=True,
        reset_runtime=True,
    )


def test_ws_command_status_is_handled(monkeypatch, tmp_path):
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)
    handled: list[str] = []

    def _handle(command: str, *, chat_id, ctx, source="", transport=None):
        del source, transport
        handled.append(command)
        ctx.send_with_budget(chat_id, "status reply")
        return True

    monkeypatch.setattr("supervisor.server_runtime.handle_slash_command", _handle)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/status"})
            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "chat" and frame.get("content") == "status reply":
                    break
            else:
                pytest.fail("expected /status command reply")

    assert handled == ["/status"]
    del runtime


def test_ws_command_queues_non_slash_commands(monkeypatch, tmp_path):
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)
    submitted: list[str] = []

    original_submit = runtime.orchestrator.submit

    def _capture_submit(envelope):
        submitted.append(envelope.effective_text())
        return original_submit(envelope)

    monkeypatch.setattr(runtime.orchestrator, "submit", _capture_submit)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/help"})
            deadline = time.monotonic() + 2.0
            while not submitted and time.monotonic() < deadline:
                time.sleep(0.01)

    assert submitted == ["/help"]
    pending = runtime.task_queue.list_pending_tasks()
    assert len(pending) == 1
    assert pending[0]["text"] == "/help"
    del runtime


def test_ws_command_policy_violation_returns_stable_error(monkeypatch, tmp_path):
    bridge = LocalChatBridge({})
    bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    monkeypatch.setattr(
        "ouroboros.gateway.browser_ws.check_operator_command_policy",
        lambda _cmd: (False, "command blocked by policy"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "rm -rf /"})
            frame = json.loads(websocket.receive_text())
            assert frame["type"] == "error"
            assert frame["ok"] is False
            assert frame["error"] == "command blocked by policy"
            assert "Traceback" not in json.dumps(frame)


def test_ws_invalid_json_returns_stable_error(monkeypatch, tmp_path):
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("{not-json")
            frame = json.loads(websocket.receive_text())
            assert frame["type"] == "error"
            assert frame["ok"] is False
            assert frame["error"]


def test_ws_unknown_type_returns_stable_error(monkeypatch, tmp_path):
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "not_a_real_type", "value": 1})
            frame = json.loads(websocket.receive_text())
            assert frame["type"] == "error"
            assert frame["ok"] is False
            assert "unknown" in frame["error"].lower()


def test_ws_command_without_orchestrator_returns_init_message(monkeypatch, tmp_path):
    reset_runtime_for_tests()
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/help"})
            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "chat" and frame.get("role") == "assistant":
                    assert "initializing" in frame["content"].lower()
                    return
            pytest.fail("expected initializing assistant chat frame")
