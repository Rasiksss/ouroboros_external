"""Transport metadata rides on the message, not in a map beside the bridge."""

from typing import Any

from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with

#: The browser conversation these replies address.
_CHAT_77 = scope_with(TEST_SCOPE, surface_id="browser", chat_id="77")
from transport.browser.contracts import TransportMetadata
from transport.interfaces import IEnvelopeBuilder, InboundEnvelope

_METADATA: TransportMetadata = {
    "kind": "messenger",
    "conversation_id": "abc",
    "sender_label": "Messenger",
}


def test_transport_metadata_contract_shape():
    payload: TransportMetadata = {"kind": "messenger", "conversation_id": "abc", "sender_label": "Messenger"}
    assert payload["conversation_id"] == "abc"


def _published(monkeypatch, tmp_path):
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests
    from transport.interfaces import SubscriptionMode

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    bridge = runtime.bridge
    events = []
    monkeypatch.setattr("supervisor.message_bus.publish_event", lambda topic, payload: events.append((topic, payload)))
    reader = runtime.gateway.outbox.reader(name="test", mode=SubscriptionMode.BROADCAST)
    return bridge, events, reader


def test_published_message_carries_the_transport_metadata_it_was_given(monkeypatch, tmp_path):
    from supervisor.runtime_context import reset_runtime_for_tests

    bridge, events, reader = _published(monkeypatch, tmp_path)
    try:
        bridge.send_message("reply", scope=_CHAT_77, source="browser", transport=dict(_METADATA))
        delivered = reader.poll(1.0)
    finally:
        reader.close()
        reset_runtime_for_tests()

    assert delivered is not None
    assert delivered.message.transport == dict(_METADATA)
    assert events[-1][1]["transport"] == dict(_METADATA)


def test_a_message_sent_without_metadata_carries_none(monkeypatch, tmp_path):
    """There is no remembered map to inherit from: the caller supplies the route."""
    from supervisor.runtime_context import reset_runtime_for_tests

    bridge, events, reader = _published(monkeypatch, tmp_path)
    try:
        bridge.send_message("reply", scope=_CHAT_77, source="browser", transport=dict(_METADATA))
        first = reader.poll(1.0)
        assert first is not None
        reader.ack(first)
        bridge.send_message("plain reply", scope=_CHAT_77, source="browser")
        second = reader.poll(1.0)
    finally:
        reader.close()
        reset_runtime_for_tests()

    assert second is not None
    assert second.message.transport == {}
    assert events[-1][1]["transport"] == {}


# -- the inbound journal ------------------------------------------------------


class _RecordBuilder(IEnvelopeBuilder):
    def build(self, record: Any) -> InboundEnvelope:
        return InboundEnvelope(scope=scope_for("browser", 1), user_id=1, text=record.content)


def _journal_row(tmp_path, *, actor_type: str, source: str, text: str, client_message_id: str, ts: str, pending: bool):
    from ouroboros.storage.chat_store import append_chat_message
    from transport.journal import UNPROCESSED_METADATA

    persisted = append_chat_message(
        tmp_path,
        scope=TEST_SCOPE,
        actor_type=actor_type,
        chat_id=1,
        user_id=1,
        text=text,
        source=source,
        client_message_id=client_message_id,
        ts=ts,
        extra_metadata=dict(UNPROCESSED_METADATA) if pending else None,
    )
    assert persisted is not None
    return persisted


def test_the_sweep_sees_past_a_window_full_of_other_actors_and_sources(tmp_path):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from transport.journal import unprocessed_records

    runtime_persistence.initialize()
    _journal_row(
        tmp_path,
        actor_type="owner",
        source="browser",
        text="never handed over",
        client_message_id="stranded",
        ts="2026-01-01T00:00:00+00:00",
        pending=True,
    )
    for index in range(6):
        _journal_row(
            tmp_path,
            actor_type="agent",
            source="browser",
            text=f"reply {index}",
            client_message_id=f"reply-{index}",
            ts=f"2026-01-01T00:01:0{index}+00:00",
            pending=False,
        )
        _journal_row(
            tmp_path,
            actor_type="owner",
            source="dialogs",
            text=f"dialogs {index}",
            client_message_id=f"dialogs-{index}",
            ts=f"2026-01-01T00:02:0{index}+00:00",
            pending=False,
        )

    found = unprocessed_records(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        source="browser",
        limit=5,
    )

    assert [record.content for record in found] == ["never handed over"]


def test_replay_builds_each_envelope_through_the_builder_interface(tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from transport.journal import replay_unprocessed

    runtime_persistence.initialize()
    _journal_row(
        tmp_path,
        actor_type="owner",
        source="browser",
        text="never handed over",
        client_message_id="stranded",
        ts="2026-01-01T00:00:00+00:00",
        pending=True,
    )
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()

    assert replay_unprocessed(gateway, tmp_path, source="browser", build=_RecordBuilder()) == 1

    assert [envelope.text for envelope in gateway._inbox.submitted] == ["never handed over"]
    assert list_owner_messages(tmp_path, scope=TEST_SCOPE)[0].metadata["inbound_processed"] is True


def test_marking_a_missing_row_processed_does_not_raise(tmp_path):
    """A failed mark costs one extra replay, which the stored identity makes harmless."""
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from transport.journal import mark_processed

    runtime_persistence.initialize()

    mark_processed(tmp_path, "no-such-row")
