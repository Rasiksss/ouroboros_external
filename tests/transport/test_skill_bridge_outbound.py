from __future__ import annotations

import pytest

from new_core.identity import SurfaceId
from new_core.skills.models import SkillId
from ouroboros.storage.chat_store import list_chat_history_messages
from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, AllowAllSkillPermissionChecker, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.interfaces import SubscriptionMode
from transport.skill_bridge import SkillBridge, SkillBridgeError

SCHEDULER_ID = "distribution:tasks-scheduler"


def _skill_bridge(tmp_path, monkeypatch, *, gateway=None):
    bridge = SkillBridge(
        tmp_path,
        gateway=gateway if gateway is not None else FakeGateway(),
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        permission_checker=AllowAllSkillPermissionChecker(),
    )
    monkeypatch.setattr(bridge, "require_permission", lambda *_args: None)
    return bridge


def test_internal_chat_counter_uses_a_path_safe_runtime_key(tmp_path, monkeypatch):
    bridge = _skill_bridge(tmp_path, monkeypatch)
    crafted = SkillId("../source", "calendar")

    bridge.allocate_internal_chat_id(str(crafted))

    counter_files = list((tmp_path / "state" / "skills").glob("*/chat_id_counter.json"))
    assert len(counter_files) == 1
    assert counter_files[0].parent.name == crafted.runtime_key
    assert not (tmp_path / "state" / "source:calendar" / "chat_id_counter.json").exists()


def test_skill_outbound_is_persisted_and_published(tmp_path, monkeypatch):
    """The answer reaches the outbox and the history the user reads."""
    from transport.interfaces import SubscriptionMode

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=runtime.gateway)
    reader = runtime.gateway.outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)

    try:
        skill_bridge.send_skill_message(
            SCHEDULER_ID,
            "Lunch time",
            chat_id=42,
            source="browser",
            task_id="schedule:slot",
        )
        delivered = reader.poll(1.0)
        messages = list_chat_history_messages(tmp_path, scope=scope_with(TEST_SCOPE, chat_id="42"))
    finally:
        reader.close()
        reset_runtime_for_tests()

    assert delivered is not None
    assert delivered.message.text == "Lunch time"
    assert delivered.message.source == "browser"
    assert messages[-1].actor_type == "agent"
    assert messages[-1].content == "Lunch time"
    assert messages[-1].channel == "browser"
    assert messages[-1].metadata["task_id"] == "schedule:slot"


def test_an_extension_send_is_refused_while_owners_are_resolved_per_person(tmp_path, monkeypatch):
    """Refused at the door, before the history write: nothing persisted, nothing published.

    An extension acts for the instance, not for a person, so the scope it would publish
    under names nobody once owners are resolved per person — the message would reach no
    connection at all. The author sees an unsupported capability rather than a message
    that vanishes.
    """
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, TrustedHeaderIdentity, browser_identity

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=runtime.gateway)
    reader = runtime.gateway.outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))

    try:
        with pytest.raises(SkillBridgeError, match="no author"):
            skill_bridge.send_skill_message(SCHEDULER_ID, "Lunch time", chat_id=42, source="browser")
        undelivered = reader.poll(0.2)
        messages = list_chat_history_messages(tmp_path, scope=TEST_SCOPE)
    finally:
        reader.close()
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))
        reset_runtime_for_tests()

    assert undelivered is None
    assert [message.content for message in messages] == []


def test_skill_outbound_is_published_to_the_outbox(tmp_path, monkeypatch):
    """The skill bridge is a producer on the seam; who delivers is decided downstream."""
    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    skill_bridge.send_skill_message(
        SCHEDULER_ID,
        "Lunch time",
        chat_id=42,
        source="browser",
        task_id="schedule:slot",
    )

    assert len(gateway.published) == 1
    published = gateway.published[0]
    assert (published.chat_id, published.text, published.source) == (42, "Lunch time", "browser")
    assert published.task_id == "schedule:slot"


def test_skill_outbound_propagates_transport_error(tmp_path, monkeypatch):
    skill_bridge = _skill_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(
        skill_bridge.gateway.outbox,
        "publish",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("transport down")),
    )

    with pytest.raises(RuntimeError, match="transport down"):
        skill_bridge.send_skill_message(
            SCHEDULER_ID,
            "Lunch time",
            chat_id=42,
            source="dialogs",
        )


def test_injected_agent_task_keeps_user_delivery_source(tmp_path, monkeypatch):
    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    skill_bridge.enqueue_skill_message(
        SCHEDULER_ID,
        "Check mail",
        chat_id=42,
        delivery_source="browser",
    )

    assert len(gateway.submitted) == 1
    envelope = gateway.submitted[0]
    assert envelope.source == "browser"
    assert envelope.sender_label == SCHEDULER_ID
    assert envelope.text == "Check mail"


def test_an_injection_without_a_delivery_source_belongs_to_the_bridge(tmp_path, monkeypatch):
    """The surface is closed, so a skill speaking for itself is the bridge; the label keeps its name."""
    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    skill_bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=42)

    assert gateway.submitted[0].source == "skill-bridge"
    assert gateway.submitted[0].sender_label == SCHEDULER_ID


def test_a_delivery_source_no_transport_claims_is_refused(tmp_path, monkeypatch):
    """``web`` was accepted and reached nobody: the answer was written to history and dropped."""
    from transport.skill_bridge import SkillBridgeError

    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    with pytest.raises(SkillBridgeError, match="unsupported delivery source"):
        skill_bridge.send_skill_message(SCHEDULER_ID, "Lunch time", chat_id=42, source="web")
    with pytest.raises(SkillBridgeError, match="unsupported delivery source"):
        skill_bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=42, delivery_source="web")

    assert gateway.published == []
    assert gateway.submitted == []


def test_an_injected_message_carries_the_delivery_surface_in_its_scope(tmp_path, monkeypatch):
    """A skill injects on behalf of a transport, so the scope is that transport's."""
    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    skill_bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=42, delivery_source="dialogs")
    skill_bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=7, delivery_source="browser")

    assert (gateway.submitted[0].scope.surface_id, gateway.submitted[0].scope.chat_id) == (SurfaceId.DIALOGS, "42")
    assert (gateway.submitted[1].scope.surface_id, gateway.submitted[1].scope.chat_id) == (SurfaceId.BROWSER, "7")


def test_an_unsupported_delivery_source_is_refused(tmp_path, monkeypatch):
    from transport.skill_bridge import SkillBridgeError

    skill_bridge = _skill_bridge(tmp_path, monkeypatch)

    with pytest.raises(SkillBridgeError, match="unsupported delivery source"):
        skill_bridge.enqueue_skill_message(
            SCHEDULER_ID,
            "Check mail",
            chat_id=42,
            delivery_source="telegram",
        )


def test_the_scope_builder_refuses_an_unknown_surface_on_its_own(tmp_path, monkeypatch):
    """The source is a skill argument, so the surface axis is coerced here, not upstream."""
    from transport.skill_bridge import SkillBridgeError, _synthetic_scope_for

    assert _synthetic_scope_for("sberchat", 42).surface_id is SurfaceId.DIALOGS
    assert _synthetic_scope_for("", 42).surface_id is SurfaceId.SKILL_BRIDGE
    with pytest.raises(SkillBridgeError, match="unsupported delivery source"):
        _synthetic_scope_for("telegram", 42)


def test_a_slash_command_from_a_skill_is_refused(tmp_path, monkeypatch):
    from transport.skill_bridge import SkillBridgeError

    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    with pytest.raises(SkillBridgeError, match="reserved for direct owner input"):
        skill_bridge.enqueue_skill_message(SCHEDULER_ID, "/status", chat_id=42)

    assert gateway.submitted == []


@parametrize_from_dict(
    {
        "text": {"text": "x" * 11, "caption": ""},
        "caption": {"text": "", "caption": "x" * 11},
        "caption with text": {"text": "ok", "caption": "x" * 11},
    }
)
def test_an_oversized_message_from_a_skill_is_refused_before_submit(tmp_path, monkeypatch, text, caption):
    from transport.skill_bridge import SkillBridgeError

    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    gateway = FakeGateway()
    skill_bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    with pytest.raises(SkillBridgeError, match="Максимальная длина — 10 символов"):
        skill_bridge.enqueue_skill_message(
            SCHEDULER_ID,
            text,
            chat_id=42,
            image_caption=caption,
            image_base64="encoded-image",
        )
    assert gateway.submitted == []


def test_every_injected_envelope_carries_a_client_message_id(tmp_path, monkeypatch):
    """At-least-once inbound is only safe because the task id is derived from this.

    An envelope without one takes a fresh task id on redelivery, so a supervisor
    that stopped between polling and handling runs the same injection twice.
    """
    gateway = FakeGateway()
    bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=42)
    bridge.enqueue_skill_message(SCHEDULER_ID, "Check mail", chat_id=42)

    ids = [envelope.client_message_id for envelope in gateway.submitted]
    assert all(ids), "a transport must set a stable client message id on every envelope"
    assert len(set(ids)) == 2, "two injections are two messages, not a redelivery of one"


def test_skill_message_exactly_at_the_limit_is_submitted(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MAX_INBOUND_MESSAGE_CHARS", "10")
    gateway = FakeGateway()
    bridge = _skill_bridge(tmp_path, monkeypatch, gateway=gateway)

    bridge.enqueue_skill_message(SCHEDULER_ID, "я" * 10, chat_id=42)

    assert gateway.submitted[0].text == "я" * 10
