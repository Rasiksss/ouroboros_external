from __future__ import annotations

import json
import threading
import time

from ouroboros.gateway import browser_ws
from supervisor.message_bus import LocalChatBridge
from supervisor.orchestrator.envelope import InboundEnvelope
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.orchestrator.orchestrator import SubmitResult
from supervisor.testing import bootstrap_supervisor_for_tests
from tests.helpers.server_client import patched_server_client


def _browser_ws_client(monkeypatch, tmp_path):
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True)


def _submit_result() -> SubmitResult:
    return SubmitResult(intent=TaskIntentKind.AGENT_CHAT, mode=SubmissionMode.QUEUE, task_id="t-coexist")


def test_browser_ws_and_headless_orchestrator_submit_without_blocking(monkeypatch, tmp_path):
    submitted: list[str] = []
    errors: list[Exception] = []
    bridge = LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    browser_ws.attach_browser_ws_bridge(bridge)

    def _capture_submit(envelope):
        submitted.append(str(envelope.source))
        return _submit_result()

    monkeypatch.setattr(runtime.orchestrator, "submit", _capture_submit)

    def _submit_headless(source: str, text: str) -> None:
        try:
            runtime.orchestrator.submit(
                InboundEnvelope.from_chat(chat_id=42, text=text, source=source),
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            headless_threads = [
                threading.Thread(target=_submit_headless, args=("dialogs", "dialogs-msg")),
                threading.Thread(target=_submit_headless, args=("a2a", "a2a-msg")),
            ]
            for thread in headless_threads:
                thread.start()

            websocket.send_json({"type": "chat", "content": "browser-msg"})
            user_frame = json.loads(websocket.receive_text())
            assert user_frame["type"] == "chat"
            assert user_frame["role"] == "user"

            for thread in headless_threads:
                thread.join(timeout=2.0)
                assert not thread.is_alive()
            deadline = time.time() + 2.0
            while "browser" not in submitted and time.time() < deadline:
                time.sleep(0.01)

    assert not errors
    assert "browser" in submitted
    assert "dialogs" in submitted
    assert "a2a" in submitted


def test_headless_gateway_minimal_while_browser_ingress_enabled(monkeypatch, tmp_path):
    from ouroboros.gateway.router import collect_routes as headless_routes

    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "true")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    paths = {getattr(route, "path", "") for route in headless_routes(data_dir=tmp_path)}
    assert {"/api/health", "/health/readiness", "/health/liveness"} <= paths
    assert "/" not in paths
    assert "/ws" not in paths
