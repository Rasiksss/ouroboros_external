from __future__ import annotations

import pytest

from ouroboros.ingress.skill_bridge import SkillBridge
from ouroboros.storage.chat_store import list_chat_history_messages
from supervisor import message_bus
from supervisor.runtime_context import reset_runtime_for_tests
from supervisor.testing import bootstrap_supervisor_for_tests


def test_skill_outbound_uses_normal_flow_and_persists_source(tmp_path, monkeypatch):
    bridge = message_bus.LocalChatBridge({})
    bootstrap_supervisor_for_tests(tmp_path, bridge=bridge)
    frames: list[dict] = []
    bridge.set_browser_sink(frames.append)
    skill_bridge = SkillBridge(tmp_path)
    monkeypatch.setattr(skill_bridge, "require_permission", lambda *_args: None)

    try:
        skill_bridge.send_skill_message(
            "tasks-scheduler",
            "Lunch time",
            chat_id=42,
            source="browser",
            task_id="schedule:slot",
        )
        messages = list_chat_history_messages(tmp_path)
    finally:
        reset_runtime_for_tests()

    assert frames[-1]["content"] == "Lunch time"
    assert frames[-1]["source"] == "browser"
    assert messages[-1].actor_type == "agent"
    assert messages[-1].content == "Lunch time"
    assert messages[-1].channel == "browser"
    assert messages[-1].metadata["task_id"] == "schedule:slot"


def test_skill_outbound_propagates_transport_error(tmp_path, monkeypatch):
    skill_bridge = SkillBridge(tmp_path)
    monkeypatch.setattr(skill_bridge, "require_permission", lambda *_args: None)
    monkeypatch.setattr(
        message_bus,
        "send_outbound_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport down")),
    )

    with pytest.raises(RuntimeError, match="transport down"):
        skill_bridge.send_skill_message(
            "tasks-scheduler",
            "Lunch time",
            chat_id=42,
            source="dialogs",
        )


def test_dialogs_outbound_fails_when_runtime_transport_is_not_ready(tmp_path, monkeypatch):
    skill_bridge = SkillBridge(tmp_path)
    monkeypatch.setattr(skill_bridge, "require_permission", lambda *_args: None)
    message_bus.configure_outbound_sender(None)

    with pytest.raises(RuntimeError, match="not ready"):
        skill_bridge.send_skill_message(
            "tasks-scheduler",
            "Lunch time",
            chat_id=42,
            source="dialogs",
        )


def test_injected_agent_task_keeps_user_delivery_source(tmp_path, monkeypatch):
    bridge = message_bus.LocalChatBridge({})
    skill_bridge = SkillBridge(tmp_path, bridge_getter=lambda: bridge)
    monkeypatch.setattr(skill_bridge, "require_permission", lambda *_args: None)

    skill_bridge.enqueue_skill_message(
        "tasks-scheduler",
        "Check mail",
        chat_id=42,
        delivery_source="browser",
    )

    update = bridge.get_updates(offset=0, timeout=1)[0]
    assert update["message"]["source"] == "browser"
    assert update["message"]["sender_label"] == "tasks-scheduler"
