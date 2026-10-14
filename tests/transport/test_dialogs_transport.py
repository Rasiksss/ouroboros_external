"""Regressions for the dialogs transport's lifecycle, delivery ladder and journal."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.dialogs_cursor import sql_dialogs_cursor_repository
from tests.helpers.fake_gateway import FakeGateway, SettlingReader
from tests.helpers.files import stored_file
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with
from transport.dialogs import transport as dialogs_transport
from transport.dialogs.messages import InboundAttachment, InboundMessage
from transport.dialogs.transport import DialogsTransport, build_dialogs_transport
from transport.interfaces import AttachmentRef, OutboundKind, OutboundMessage


class _Provider:
    def __init__(self, *, text_result=(True, "ok"), file_result=(True, "file-mid"), can_send: bool = True) -> None:
        self.text_result = text_result
        self.file_result = file_result
        self.can_send = can_send
        self.connect_calls = 0
        self.text_calls: list[tuple[Any, str]] = []
        self.file_calls: list[tuple[Any, bytes]] = []
        self.closed = False

    def connect(self) -> None:
        self.connect_calls += 1

    def authenticate(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def can_send_to_chat_id(self, _chat_id) -> bool:
        return self.can_send

    def send_text(self, chat_id, text: str, fmt: str = "", **_kwargs):
        del fmt
        self.text_calls.append((chat_id, text))
        return self.text_result

    def send_file(self, chat_id, file_bytes, **_kwargs):
        self.file_calls.append((chat_id, file_bytes))
        return self.file_result

    def recv_update(self, _cursor, timeout_sec: float = 0):
        del timeout_sec
        # A real update stream blocks; returning instantly would spin the loop.
        time.sleep(0.02)
        return None, 0


def _settings(**overrides) -> AppSettings:
    data: dict[str, Any] = {
        "DIALOGS_GRPC_ENDPOINT": "grpc://dialogs",
        "DIALOGS_BOT_TOKEN": SecretStr("token"),
    }
    data.update(overrides)
    return AppSettings(**data)


def _transport(tmp_path, provider: Any, *, gateway=None, settings=None) -> DialogsTransport:
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.initialize()
    return DialogsTransport(
        settings=settings if settings is not None else _settings(),
        gateway=gateway if gateway is not None else FakeGateway(),
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="sess-test",
        provider_factory=lambda _settings: provider,
        cursor_repository=sql_dialogs_cursor_repository(),
    )


def _outbound(**kwargs) -> OutboundMessage:
    kwargs.setdefault("source", "dialogs")
    kwargs.setdefault("scope", scope_for(kwargs["source"], kwargs.pop("chat_id", "peer-42")))
    return OutboundMessage(**kwargs)


def _stored_attachment(tmp_path, *, data: bytes = b"pdf") -> AttachmentRef:
    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=data,
        attachment_id=uuid.uuid4().hex,
        filename="report.pdf",
        mime="application/pdf",
        source="dialogs",
    )
    return AttachmentRef(attachment_id=str(metadata["id"]), filename="report.pdf", mime="application/pdf")


def _await(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- a provider outage is transient, an unknown peer is not -------------------


def test_a_provider_outage_keeps_the_reply_in_the_durable_subscription(tmp_path, monkeypatch):
    """A five-second blip must not acknowledge away everything published in it."""
    monkeypatch.setattr(dialogs_transport, "_DELIVERY_RETRY_BACKOFF_SEC", 0.0)

    def _unreachable(_settings):
        raise ConnectionError("grpc endpoint is down")

    transport = _transport(tmp_path, None)
    transport.provider_factory = _unreachable
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(text="the answer"))

    transport.handle_outbound(reader, delivered)

    assert (reader.acked, reader.nacked) == (0, 1)


def test_a_provider_outage_drops_the_message_only_through_the_attempt_ladder(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(dialogs_transport, "_DELIVERY_RETRY_BACKOFF_SEC", 0.0)
    transport = _transport(tmp_path, None)
    transport.provider_factory = lambda _settings: (_ for _ in ()).throw(ConnectionError("down"))
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(text="the answer"))

    with caplog.at_level(logging.ERROR, logger="transport.dialogs.transport"):
        for attempts in range(dialogs_transport._DELIVERY_ATTEMPT_LIMIT):
            transport.handle_outbound(reader, replace(delivered, attempts=attempts))

    assert (reader.acked, reader.nacked) == (1, 2)
    assert "Giving up on a dialogs message" in caplog.text


def test_a_live_provider_that_does_not_know_the_peer_drops_the_message(tmp_path, caplog):
    transport = _transport(tmp_path, _Provider(can_send=False))
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(text="the answer"))

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.transport"):
        transport.handle_outbound(reader, delivered)

    assert (reader.acked, reader.nacked) == (1, 0)
    assert "unreachable chat" in caplog.text


def test_a_message_whose_chat_is_not_a_number_is_dropped_by_the_peer_lookup(tmp_path, caplog):
    """A browser conversation's opaque id was never this transport's message.

    The peer table is keyed by the chat id the provider knows, so an id it does not hold
    misses like any other unreachable peer — the reader that needs a number decides on its
    own side, and dropping it costs one message rather than re-pointing a row at a peer.
    """
    provider = _Provider()
    provider._known = {"peer-42"}
    provider.can_send_to_chat_id = lambda chat_id: str(chat_id) in provider._known
    transport = _transport(tmp_path, provider)
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(text="the answer", chat_id="3f0a9c1e-5f4d-4a1b"))

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.transport"):
        transport.handle_outbound(reader, delivered)

    assert (reader.acked, reader.nacked) == (1, 0)
    assert "unreachable chat" in caplog.text
    assert provider.text_calls == []


def test_a_delivery_in_flight_at_shutdown_goes_back_to_the_subscription(tmp_path):
    """`close` sets `closed` before joining the reader, so its message is not lost."""
    transport = _transport(tmp_path, _Provider())
    transport.ensure_provider()
    transport.close()
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(text="published just before shutdown"))

    transport.handle_outbound(reader, delivered)

    assert (reader.acked, reader.nacked) == (0, 1)


# -- shutdown cannot wait on a network round-trip -----------------------------


def test_close_returns_while_a_connect_holds_the_provider_lock(tmp_path):
    transport = _transport(tmp_path, _Provider())
    transport.ensure_provider()
    holding = threading.Event()
    release = threading.Event()

    def _hold_the_lock() -> None:
        with transport._provider_lock:
            holding.set()
            release.wait(30)

    holder = threading.Thread(target=_hold_the_lock, daemon=True)
    holder.start()
    assert holding.wait(5)

    closed = threading.Event()

    def _close() -> None:
        try:
            transport.close()
        except TimeoutError:
            pass
        finally:
            closed.set()

    threading.Thread(target=_close, daemon=True).start()
    try:
        assert closed.wait(dialogs_transport._PROVIDER_CLOSE_LOCK_TIMEOUT_SEC + 5), "close blocked on the provider lock"
    finally:
        release.set()
        holder.join(timeout=5)

    assert transport.closed is True
    assert transport._stop_event.is_set()


def test_ensure_provider_gives_up_once_shutdown_has_begun(tmp_path):
    provider = _Provider()
    transport = _transport(tmp_path, provider)
    transport._stop_event.set()

    assert transport.ensure_provider() is None
    assert provider.connect_calls == 0


# -- the subscription exists as soon as the transport does --------------------


def test_a_message_published_before_start_still_reaches_the_transport(tmp_path):
    gateway = FakeGateway()
    provider = _Provider()
    transport = _transport(tmp_path, provider, gateway=gateway)

    gateway.answer("peer-42", "published before start", source="dialogs")
    transport.start()
    try:
        assert _await(lambda: provider.text_calls), "the pre-start publish never arrived"
    finally:
        transport.close()

    assert provider.text_calls[0][1] == "published before start"


def test_close_is_safe_for_a_transport_that_was_never_started(tmp_path):
    transport = _transport(tmp_path, _Provider())

    transport.close()
    transport.close()

    assert transport._reader is None


# -- an attachment that never left is retried, then named in the audit trail --


def test_a_failing_attachment_delivery_is_retried_then_named_when_dropped(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(dialogs_transport, "_DELIVERY_RETRY_BACKOFF_SEC", 0.0)
    provider = _Provider(file_result=(False, "sdk refused the upload"))
    transport = _transport(tmp_path, provider)
    reference = _stored_attachment(tmp_path)
    reader = SettlingReader()
    delivered = reader.deliver(_outbound(kind=OutboundKind.FILE, attachment=reference, task_id="task-7"))

    with caplog.at_level(logging.ERROR, logger="transport.dialogs.transport"):
        for attempts in range(dialogs_transport._DELIVERY_ATTEMPT_LIMIT):
            transport.handle_outbound(reader, replace(delivered, attempts=attempts))

    assert len(provider.file_calls) == dialogs_transport._DELIVERY_ATTEMPT_LIMIT
    assert (reader.acked, reader.nacked) == (1, 2)
    assert "task_id=task-7" in caplog.text
    assert f"attachment_id={reference.attachment_id}" in caplog.text


# -- a refusal nobody received leaves the update for a retry ------------------


def _command_with_a_file(tmp_path) -> tuple[InboundMessage, Path]:
    staged = tmp_path / "report.pdf"
    staged.write_bytes(b"pdf")
    message = InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="/status",
        source_message_id="mid-refusal",
        attachments=(InboundAttachment(filename="report.pdf", mime="application/pdf", size=3, temp_path=str(staged)),),
    )
    return message, staged


def test_an_undelivered_refusal_keeps_the_update_and_its_staged_files(tmp_path, caplog):
    provider = _Provider(text_result=(False, "dialogs peer is not available"))
    transport = _transport(tmp_path, provider)
    message, staged = _command_with_a_file(tmp_path)

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.transport"), pytest.raises(RuntimeError):
        transport.handle_inbound(message)

    assert staged.exists(), "the staged file must survive for the retry"
    assert transport.cursor.value == 0
    assert transport.gateway.submitted == []
    assert "refusal undelivered" in caplog.text


def test_a_delivered_refusal_lets_the_update_be_committed(tmp_path):
    provider = _Provider()
    transport = _transport(tmp_path, provider)
    message, _staged = _command_with_a_file(tmp_path)

    transport.handle_inbound(message)

    assert provider.text_calls[0][1].startswith(dialogs_transport.COMMAND_WITH_FILES_REFUSAL)
    assert transport.gateway.submitted == []


# -- the shared reader loop and the shared journal helpers --------------------


def test_the_outbox_reader_runs_the_shared_loop(tmp_path, caplog):
    """The loop that holds the seam's invariants is the one in transport.reader_loop."""
    transport = _transport(tmp_path, _Provider())

    def _boom(_timeout: float = 0.0):
        raise RuntimeError("backend hiccup")

    transport._reader.poll = _boom  # type: ignore[union-attr]
    caplog.clear()  # the store migration logged during setup
    with caplog.at_level(logging.ERROR, logger="transport.reader_loop"):
        transport._read_outbox()

    assert [record.name for record in caplog.records] == ["transport.reader_loop"]


def test_a_reader_that_gave_up_releases_its_durable_subscription(tmp_path):
    """The shared loop leaves closing to its caller, and a stopped reader must close.

    The subscription is durable: left open with nothing draining it, every
    publish still fans into it until the drop limit.
    """
    gateway = FakeGateway()
    transport = _transport(tmp_path, _Provider(), gateway=gateway)

    def _boom(_timeout: float = 0.0):
        raise RuntimeError("backend hiccup")

    transport._reader.poll = _boom  # type: ignore[union-attr]
    transport._read_outbox()

    gateway.answer("peer-42", "after the reader died", source="dialogs")
    assert gateway.outbox._outbox._shared == {}, "a subscription nothing drains is a write-only sink"


def test_the_journal_sweep_builds_envelopes_through_the_interface(tmp_path):
    from ouroboros.storage.chat_store import append_chat_message
    from transport.journal import UNPROCESSED_METADATA

    transport = _transport(tmp_path, _Provider())
    append_chat_message(
        tmp_path,
        scope=scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="peer-42"),
        actor_type="owner",
        chat_id="peer-42",
        user_id="user-42",
        text="never handed over",
        source="dialogs",
        client_message_id="mid-stranded",
        extra_metadata=dict(UNPROCESSED_METADATA),
    )

    assert transport.resubmit_unprocessed() == 1
    assert [envelope.text for envelope in transport.gateway.submitted] == ["never handed over"]


# -- collaborators are required, and nothing mirrors the provider -------------


def test_the_transport_stamps_the_session_it_was_given(tmp_path):
    """The session is a startup constant, so an inbound message costs no store read."""
    transport = _transport(tmp_path, _Provider())
    transport._session_id = "sess-once"

    transport.handle_inbound(
        InboundMessage(chat_id="peer-42", user_id="user-42", text="hello", source_message_id="mid-1")
    )

    from ouroboros.storage.chat_store import list_owner_messages

    rows = list_owner_messages(tmp_path, scope=scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="peer-42"))
    assert [row.session_id for row in rows if row.content == "hello"] == ["sess-once"]
    assert transport.gateway.submitted[0].text == "hello"


def test_an_inbound_message_originates_a_well_formed_trace_id(tmp_path, caplog):
    """Dialogs has no inbound carrier — a gRPC update stream has no header frame — so it mints.
    An origination is not a rejection, so nothing is warned about."""
    from ouroboros.observability import aef_tracing

    transport = _transport(tmp_path, _Provider())

    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        transport.handle_inbound(
            InboundMessage(chat_id="peer-42", user_id="user-42", text="hello", source_message_id="mid-1")
        )

    trace_id = transport.gateway.submitted[0].trace_id
    assert aef_tracing.normalize_trace_id(trace_id) == trace_id
    # Scoped to the tracing logger: caplog captures every logger, and an unrelated warning
    # from somewhere else in ingress says nothing about whether a trace id was rejected.
    assert not [record for record in caplog.records if record.name == aef_tracing.log.name]


def test_the_transport_requires_its_collaborators(tmp_path):
    with pytest.raises(TypeError, match="storage"):
        DialogsTransport(  # type: ignore[call-arg]
            settings=_settings(),
            gateway=FakeGateway(),
            drive_root=tmp_path,
            session_id="sess-test",
            provider_factory=lambda _settings: _Provider(),
        )
    with pytest.raises(TypeError, match="storage"):
        build_dialogs_transport(  # type: ignore[call-arg]
            settings=_settings(),
            gateway=FakeGateway(),
            drive_root=tmp_path,
            session_id="sess-test",
            provider_factory=lambda _settings: _Provider(),
        )


def test_provider_active_is_derived_not_mirrored(tmp_path):
    transport = _transport(tmp_path, _Provider())

    assert transport.provider_active is False
    transport.ensure_provider()
    assert transport.provider_active is True
    with pytest.raises(AttributeError):
        transport.provider_active = True  # type: ignore[misc]


def test_an_inbound_message_keeps_the_operation_its_ingress_established(tmp_path):
    """One update, one operation: the submit continues the leg rather than starting a second."""
    from ouroboros.observability import aef_tracing

    transport = _transport(tmp_path, _Provider())
    trace_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="hello",
            source_message_id="mid-keep",
            trace_id=trace_id,
        )
    )

    assert transport.gateway.submitted[0].trace_id == trace_id
    assert aef_tracing.current_trace_id() == ""
