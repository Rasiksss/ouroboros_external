"""The seam's payload round trip: declared types back, not an equal blob of JSON.

Every reader dispatches on ``OutboundKind`` by identity, reads ``chat_id`` off the
scope and the attachment by attribute. A decode that hands back a string, a
mapping and a mapping passes ``==`` and breaks all three, and
``transport/reader_loop.py`` turns the resulting exception into ack-and-drop — so
the failure this file names is a photo that disappears with a log line.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from new_core.identity import OwnerScope
from tests.helpers.transport_conformance import full_envelope, full_message
from transport.codec import (
    decode_inbound,
    decode_outbound,
    encode_inbound,
    encode_outbound,
    ensure_wire_safe,
)
from transport.infra.inmemory import InProcessInbox, InProcessOutbox
from transport.interfaces import (
    AttachmentRef,
    InboundEnvelope,
    OutboundKind,
    OutboundMessage,
    SubscriptionMode,
    routed_chat_id,
)


def _round_trip_outbound(message: OutboundMessage) -> OutboundMessage:
    """Through JSON and back, so the wire form is proven to be a wire form."""
    return decode_outbound(json.loads(json.dumps(encode_outbound(message))))


def _round_trip_inbound(envelope: InboundEnvelope) -> InboundEnvelope:
    return decode_inbound(json.loads(json.dumps(encode_inbound(envelope))))


def test_an_outbound_payload_returns_as_its_declared_types() -> None:
    message = full_message()

    decoded = _round_trip_outbound(message)

    assert decoded == message
    assert decoded.kind is OutboundKind.PHOTO
    assert decoded.kind.value == "photo"
    assert isinstance(decoded.scope, OwnerScope)
    assert decoded.chat_id == 4242
    assert isinstance(decoded.attachment, AttachmentRef)
    assert decoded.attachment.attachment_id == "att-1"
    assert decoded.attachment.size == 1024
    assert decoded.transport == message.transport


def test_an_inbound_payload_returns_as_its_declared_types() -> None:
    envelope = full_envelope()

    decoded = _round_trip_inbound(envelope)

    assert decoded == envelope
    assert isinstance(decoded.scope, OwnerScope)
    assert decoded.chat_id == "4242"
    assert decoded.source == "browser"
    assert decoded.attachments == envelope.attachments
    assert decoded.attachment_ids == ["att-1", "att-2"]


def test_the_chat_axis_crosses_the_wire_as_a_string() -> None:
    """a2a drops any message whose derived chat id is not an int, so one conversion only."""
    decoded = _round_trip_outbound(full_message(chat_id="4242"))

    assert decoded.scope.chat_id == "4242"
    assert routed_chat_id(decoded.scope) == 4242


def test_a_browser_conversation_survives_the_round_trip_verbatim() -> None:
    """The browser boundary admits an opaque key, and two spellings there are two chats."""
    for chat_id in ("3f0a9c1e-5f4d-4a1b", "01", "новый", "0"):
        decoded = _round_trip_outbound(full_message(chat_id=chat_id))
        assert decoded.scope.chat_id == chat_id


@pytest.mark.parametrize(
    ("axis", "expected"),
    [("4242", 4242), ("0", 0), ("-4001", -4001), ("01", "01"), ("+7", "+7"), ("seven", "seven"), ("1.5", "1.5")],
    ids=["canonical", "zero", "negative", "padded", "signed", "word", "float"],
)
def test_the_conversion_narrows_only_a_canonical_decimal_spelling(axis: str, expected: object) -> None:
    """Plain ``int()`` would fold ``"01"`` to ``1``, so a row keyed ``"01"`` would record ``1``."""
    assert routed_chat_id(full_message(chat_id=axis).scope) == expected


def test_a_kind_naming_no_member_fails_to_decode() -> None:
    payload = encode_outbound(full_message())
    payload["kind"] = "hologram"

    with pytest.raises(ValueError):
        decode_outbound(payload)


def test_an_attachment_that_cannot_be_reconstructed_fails_to_decode() -> None:
    payload = encode_outbound(full_message())
    payload["attachment"] = "att-1"

    with pytest.raises(TypeError):
        decode_outbound(payload)


def test_a_message_a_newer_instance_published_still_decodes() -> None:
    """Instances upgrade one at a time; a strict decode would drop user messages."""
    payload = encode_outbound(full_message())
    payload["priority"] = "high"
    del payload["caption"]

    decoded = decode_outbound(payload)

    assert decoded.text == "the answer"
    assert decoded.caption == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("is_progress", "false"),
        ("text", {"a": 1}),
        ("task_id", 17),
        ("attachment", {"attachment_id": "att-1", "size": "big"}),
    ],
)
def test_a_field_that_is_not_its_declared_type_fails_to_decode(field: str, value: object) -> None:
    """A wrong object is worse than a failure: `"false"` is truthy, and a string iterates."""
    payload = encode_outbound(full_message())
    payload[field] = value

    with pytest.raises(TypeError):
        decode_outbound(payload)


def test_encoding_does_not_alias_the_callers_containers() -> None:
    """A backend stamping a header into the encoded mapping must not touch the payload."""
    message = full_message()

    encoded = encode_outbound(message)
    encoded["transport"]["hop"] = "instance-2"
    encoded["transport"]["route"]["tags"].append("added")

    assert "hop" not in message.transport
    assert message.transport["route"]["tags"] == ["a", "b"]


def test_decoding_does_not_alias_the_wire_buffer() -> None:
    wire = encode_outbound(full_message())

    decoded = decode_outbound(wire)
    decoded.transport["hop"] = "instance-2"

    assert "hop" not in wire["transport"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_a_non_finite_float_is_refused(value: float) -> None:
    """json.dumps writes a bare NaN, which is not JSON — and NaN != NaN breaks equality."""
    message = full_message()
    message.transport["score"] = value

    with pytest.raises(TypeError, match="non-finite"):
        ensure_wire_safe(message)


def test_a_scope_missing_an_axis_fails_to_decode() -> None:
    payload = encode_outbound(full_message())
    payload["scope"] = {"owner_id": "o", "surface_id": "browser", "chat_id": "1"}

    with pytest.raises(ValueError):
        decode_outbound(payload)


@pytest.mark.parametrize(
    "value",
    [pathlib.Path("/tmp/x"), datetime.datetime(2026, 8, 25), {"a", "b"}, b"bytes", ("a", "b")],
    ids=["path", "datetime", "set", "bytes", "tuple"],
)
def test_publish_refuses_a_value_no_wire_format_carries(value: object) -> None:
    outbox = InProcessOutbox()
    reader = outbox.reader(name="browser", mode=SubscriptionMode.BROADCAST)
    message = full_message()
    message.transport["hint"] = value

    try:
        with pytest.raises(TypeError):
            outbox.publish(message)
        assert reader.poll(0.0) is None, "a refused payload must not be queued"
    finally:
        reader.close()


@pytest.mark.parametrize("field", ["transport"])
def test_submit_refuses_a_value_no_wire_format_carries(field: str) -> None:
    inbox = InProcessInbox()
    envelope = full_envelope()
    getattr(envelope, field)["hint"] = pathlib.Path("/tmp/x")

    with pytest.raises(TypeError):
        inbox.submit(envelope)
    assert inbox.poll(0.0) is None


def test_submit_refuses_an_unwireable_value_nested_in_attachments() -> None:
    inbox = InProcessInbox()
    envelope = full_envelope()
    envelope.attachments[0]["path"] = pathlib.Path("/tmp/x")

    with pytest.raises(TypeError):
        inbox.submit(envelope)


def test_the_refusal_names_the_field_path_and_the_type() -> None:
    message = full_message()
    message.transport["route"]["tags"].append(pathlib.Path("/tmp/x"))

    with pytest.raises(TypeError) as raised:
        ensure_wire_safe(message)

    text = str(raised.value)
    assert "transport['route']['tags'][2]" in text
    assert "PosixPath" in text or "Path" in text


def test_a_non_string_key_is_refused_by_name() -> None:
    """JSON turns an integer key into a string; a silent reshape is not a round trip."""
    message = full_message()
    message.transport[17] = "reply"  # type: ignore[index]

    with pytest.raises(TypeError) as raised:
        ensure_wire_safe(message)

    assert "non-string key" in str(raised.value)


def test_nested_plain_values_are_accepted_and_survive() -> None:
    message = full_message()
    message.transport["route"]["nested"] = {"levels": [{"deep": [1, 2.5, True, None]}]}

    ensure_wire_safe(message)
    decoded = _round_trip_outbound(message)

    assert decoded.transport == message.transport
