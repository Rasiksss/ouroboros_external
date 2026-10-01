import pytest

import ouroboros.observability.event_bus as event_bus
import supervisor.message_bus as message_bus


def _make_bridge(monkeypatch, settings=None):
    return message_bus.LocalChatBridge(settings or {})


def test_configure_from_settings_without_legacy_field(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    bridge.configure_from_settings(
        {
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "999",
        }
    )
    assert bridge.get_updates(offset=0, timeout=0) == []


def test_enqueue_local_message_structured_fields(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    bridge.enqueue_local_message(
        "hello",
        source="ingress",
        sender_session_id="sess-1",
        client_message_id="c-1",
    )
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["text"] == "hello"
    assert updates[0]["message"]["source"] == "ingress"
    assert updates[0]["message"]["sender_session_id"] == "sess-1"
    assert updates[0]["message"]["client_message_id"] == "c-1"


def test_enqueue_local_message_preserves_suppress_chat_log_flag(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    bridge.enqueue_local_message("FULL_PROMPT", suppress_chat_log=True)
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["text"] == "FULL_PROMPT"
    assert updates[0]["message"]["suppress_chat_log"] is True


def test_send_photo_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_photo(123, b"img", caption="caption", mime="image/png")

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_PHOTO
    assert payload["image_base64"]
    assert payload["caption"] == "caption"
    assert payload["mime"] == "image/png"
    assert payload["source"] == ""


def test_browser_sink_receives_only_browser_typing_and_photos(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    frames = []
    bridge.set_browser_sink(frames.append)

    bridge.send_chat_action(1, source="dialogs")
    bridge.send_photo(1, b"dialogs", source="dialogs")
    bridge.send_chat_action(1, source="browser")
    bridge.send_photo(1, b"browser", source="browser")

    assert [frame["type"] for frame in frames] == ["typing", "photo"]


@pytest.mark.parametrize(
    "send",
    [
        lambda bridge: message_bus.send_with_budget("42", "ответ", source="dialogs", task_id="task-1"),
        lambda bridge: message_bus.send_with_budget(
            "42", "работаю", source="dialogs", task_id="task-1", is_progress=True
        ),
        lambda bridge: message_bus.send_with_budget("42", "статус: ок", source="dialogs"),
        lambda bridge: bridge.send_photo("42", b"png-bytes", caption="картинка", source="dialogs"),
        lambda bridge: bridge.send_file("42", b"file-bytes", filename="report.txt", caption="файл", source="dialogs"),
        lambda bridge: message_bus.send_with_budget("42", "ответ", source="sberchat", task_id="task-1"),
    ],
    ids=["text", "progress", "runtime-notice", "image", "file", "legacy-sberchat"],
)
def test_every_outbound_kind_joins_the_chat_conversation(tmp_path, send):
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    bridge = message_bus.LocalChatBridge({})
    bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)

    try:
        # The Dialogs ingress supplies the chat conversation explicitly
        # (the store itself is transport-neutral).
        append_chat_message(
            tmp_path,
            actor_type="owner",
            chat_id="42",
            user_id="7",
            text="сделай",
            session_id="session-1",
            source="dialogs",
            conversation_id="dialogs:42",
        )
        send(bridge)
    finally:
        reset_runtime_for_tests()

    stored = [
        (message.actor_type, message.conversation_id)
        for message in list_owner_messages(tmp_path, include_progress=True)
    ]
    assert stored == [("owner", "dialogs:42"), ("agent", "dialogs:42")]


def test_transportless_progress_does_not_invent_a_dialogs_conversation(tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    bridge = message_bus.LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    try:
        session_id = str(runtime.state_repo.load().session_id or "")
        message_bus.send_with_budget(
            0,
            "Skill install: `demo` — running",
            is_progress=True,
            task_id="skill_lifecycle_install_demo",
            source="internal",
        )
    finally:
        reset_runtime_for_tests()

    stored = list_owner_messages(tmp_path, include_progress=True)
    assert [message.conversation_id for message in stored] == [session_id]


def test_send_with_budget_supports_string_owner_ids(tmp_path):
    from supervisor.runtime_context import reset_runtime_for_tests
    from supervisor.testing import bootstrap_supervisor_for_tests

    bridge = message_bus.LocalChatBridge({})
    runtime = bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    state = runtime.state_repo.load()
    state.owner_id = "user-42"
    state.owner_chat_id = "peer-42"
    runtime.state_repo.save(state)
    frames = []
    bridge.set_browser_sink(frames.append)

    try:
        message_bus.send_with_budget(
            "peer-42",
            "string id reply",
            source="browser",
        )
    finally:
        reset_runtime_for_tests()

    assert frames[0]["content"] == "string id reply"
