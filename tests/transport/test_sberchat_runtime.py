from __future__ import annotations

import contextlib
import logging
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import SecretStr

from new_core.identity import SurfaceId
from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.attachment_validation import attachment_mime_for_suffix
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.blob_double import recording_blob_double
from tests.helpers.dialogs_cursor import sql_dialogs_cursor_repository
from tests.helpers.files import stored_file
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import supervisor_heartbeat
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from transport.dialogs import outbound as provider_module

#: The dialogs conversation the bridge cases address.
_PEER_42 = scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="peer-42")
from ouroboros.agent_events.interfaces import OutboundText, TypingStopped
from tests.helpers.fake_gateway import RecordingOutbox
from transport.dialogs.messages import InboundAttachment, InboundMessage

#: Bounds an assertion, not the behavior under test: a passed file reaches the inbound
#: queue only after ``validate_attachment_path`` spawns its parser child, and that spawn
#: costs a full interpreter start — under a second here, seconds on a loaded CI runner.
FILE_PIPELINE_TIMEOUT = 30.0


def test_dialogs_package_exports_provider() -> None:
    from transport.dialogs import DialogsProvider
    from transport.dialogs.provider import _dialogs_bot_token

    assert DialogsProvider.name == "dialogs"
    assert callable(_dialogs_bot_token)


def _dialogs_settings(**overrides) -> AppSettings:
    data = {
        "DIALOGS_GRPC_ENDPOINT": "",
        "DIALOGS_BOT_TOKEN": SecretStr(""),
        "DIALOGS_GROUP_ID": "",
        "DIALOGS_USER_ID": "",
        "DIALOGS_SECURE": True,
        "DIALOGS_ROOT_CERTIFICATES": "",
    }
    data.update(overrides)
    if isinstance(data["DIALOGS_BOT_TOKEN"], str):
        data["DIALOGS_BOT_TOKEN"] = SecretStr(data["DIALOGS_BOT_TOKEN"])
    return AppSettings(**data)


def _dialogs_file_update(
    *,
    message_id: str = "mid-1",
    filename: str = "report.txt",
    mime: str = "text/plain",
    size: int = 0,
    peer_id: str = "peer-42",
    peer_type: int = 1,
    user_id: str = "user-42",
):
    return SimpleNamespace(
        peer=SimpleNamespace(id=peer_id, type=peer_type),
        sender_peer=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            mid=message_id,
            document_message=SimpleNamespace(
                file_id=10,
                access_hash=20,
                name=filename,
                mime_type=mime,
                size=size,
            ),
        ),
    )


def _dialogs_text_update(
    text: str,
    *,
    message_id: str = "mid-1",
    peer=None,
    user_id: str = "user-42",
):
    peer = peer or SimpleNamespace(id="peer-42", type=1)
    return SimpleNamespace(
        peer=peer,
        sender_peer=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            mid=message_id,
            text_message=SimpleNamespace(text=text),
        ),
    )


def _dialogs_transport(tmp_path, provider=None, *, gateway=None, cursor_repository=None, **settings):
    """A transport wired to a fake gateway: no supervisor, no live bridge."""
    from tests.helpers.fake_gateway import FakeGateway
    from transport.dialogs import build_dialogs_transport

    runtime_persistence.initialize()
    return build_dialogs_transport(
        settings=_dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            **settings,
        ),
        gateway=gateway if gateway is not None else FakeGateway(),
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="sess-test",
        provider_factory=lambda _cfg: provider,
        cursor_repository=cursor_repository if cursor_repository is not None else sql_dialogs_cursor_repository(),
    )


def _outbound(chat_id="peer-42", **kwargs):
    from tests.helpers.scope import scope_for
    from transport.interfaces import OutboundMessage

    kwargs.setdefault("source", "dialogs")
    kwargs.setdefault("scope", scope_for(kwargs["source"], chat_id))
    return OutboundMessage(**kwargs)


def _stored_media(data: bytes, filename: str) -> StoredFile:
    metadata = stored_file(
        scope=_PEER_42,
        data=data,
        filename=filename,
        mime=attachment_mime_for_suffix(filename),
    )
    return StoredFile(metadata["id"])


class _Delivery:
    """One published message plus the reader the transport settles it through."""

    def __init__(self, message):
        from tests.helpers.fake_gateway import SettlingReader

        self.reader = SettlingReader()
        self.delivered = self.reader.deliver(message)

    @property
    def acked(self):
        return self.reader.acked

    @property
    def nacked(self):
        return self.reader.nacked


def _sdk_provider(messaging, **settings):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            **settings,
        )
    )
    provider._bot = SimpleNamespace(
        messaging=messaging,
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )
    provider._out_peer_by_chat_id["peer-42"] = SimpleNamespace(id="peer-42")
    return provider


class _SdkUUID:
    def __init__(self, msb, lsb):
        self.msb = msb
        self.lsb = lsb

    def __str__(self):
        return f"{self.msb}:{self.lsb}"


def _install_sdk_uuid(monkeypatch):
    package = ModuleType("dialog_bot_sdk")
    package.__path__ = []
    entities = ModuleType("dialog_bot_sdk.entities")
    entities.__path__ = []
    definitions = ModuleType("dialog_bot_sdk.entities.definitions")
    definitions.UUID = _SdkUUID
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk", package)
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities", entities)
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.definitions", definitions)


def _stub_attachment_parser(monkeypatch):
    """Keep the out-of-process document parser out of transport tests.

    ``validate_attachment_path`` spawns a fresh interpreter per document; on a loaded
    runner that spawn alone outlasts the seconds these tests give the queue.
    """
    from ouroboros.storage import attachment_validation as attachments

    monkeypatch.setattr(attachments, "run_parser", lambda *_args, **_kwargs: "")


def _download_provider(download, **settings):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(**settings))
    provider._bot = SimpleNamespace(
        internal=SimpleNamespace(downloading=SimpleNamespace(download_file_sync=download)),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )
    return provider


def _file_message(path: Path, *, message_id: str = "mid-1") -> InboundMessage:
    return InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="Проверь отчет",
        source_message_id=message_id,
        attachments=(
            InboundAttachment(
                filename="report.pdf",
                mime="application/pdf",
                size=path.stat().st_size,
                temp_path=str(path),
            ),
        ),
    )


class _DummyProvider:
    def __init__(self, messages=None, recv_cursor=0):
        self.messages = list(messages or [])
        self.recv_cursor = recv_cursor
        self.connect_calls = 0
        self.authenticate_calls = 0
        self.text_calls = []
        self.image_calls = []
        self.file_calls = []

    def connect(self):
        self.connect_calls += 1

    def authenticate(self):
        self.authenticate_calls += 1

    def send_text(self, chat_id, text: str, fmt: str = "", **kwargs):
        self.text_calls.append((chat_id, text, fmt, kwargs))
        return True, "ok"

    def recv_update(self, cursor, timeout_sec: int = 0):
        if not self.messages:
            return None, self.recv_cursor
        return self.messages.pop(0), self.recv_cursor

    def send_image(self, chat_id, image_bytes, **kwargs):
        self.image_calls.append((chat_id, image_bytes, kwargs))
        return True, "image-mid"

    def send_file(self, chat_id, file_bytes, **kwargs):
        self.file_calls.append((chat_id, file_bytes, kwargs))
        return True, "file-mid"

    def can_send_to_chat_id(self, _chat_id):
        return True


class _DummyBridge:
    def send_message(self, *_args, **_kwargs):
        return True, "ok"

    def send_chat_action(self, *_args, **_kwargs):
        return True

    def send_photo(self, *_args, **_kwargs):
        return True, "ok"


def test_dialogs_message_split_uses_safe_3800_char_limit():
    from transport.dialogs.outbound import (
        MAX_DIALOGS_MESSAGE_CHARS,
        _split_dialogs_message,
    )

    chunks = _split_dialogs_message("x" * 7601)

    assert MAX_DIALOGS_MESSAGE_CHARS == 3800
    assert len(chunks) == 3
    assert max(len(chunk) for chunk in chunks) <= 3800


def test_dialogs_runtime_disabled_without_endpoint_or_token(tmp_path):
    from tests.helpers.fake_gateway import FakeGateway
    from transport.dialogs import build_dialogs_transport

    runtime_persistence.initialize()
    transport = build_dialogs_transport(
        settings=_dialogs_settings(),
        gateway=FakeGateway(),
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="sess-test",
        provider_factory=lambda _cfg: (_ for _ in ()).throw(AssertionError("provider must stay disabled")),
        cursor_repository=sql_dialogs_cursor_repository(),
    )

    assert transport.provider_active is False
    assert transport.provider is None


def test_dialogs_transport_starts_provider_and_delivers_a_published_answer(tmp_path):
    """Outbound arrives over the seam, not through a patched supervisor call."""
    provider = _DummyProvider()
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="hello", markdown=True))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert transport.provider_active is True
    assert provider.connect_calls == 1
    assert provider.authenticate_calls == 1
    assert provider.text_calls == [("peer-42", "hello", "markdown", {})]
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_transport_lifecycle_logs_only_state_changes(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    transport = _dialogs_transport(tmp_path, _DummyProvider())
    transport.ensure_provider()
    transport.ensure_provider()
    transport.close()
    transport.close()

    assert caplog.text.count("event=dialogs_transport_started") == 1
    assert caplog.text.count("event=dialogs_transport_stopped") == 1
    assert "Dialogs transport reusing existing provider" not in caplog.text


def test_dialogs_updates_reconnect_resets_failed_registration_and_logs_only_transitions(caplog):
    from transport.dialogs import DialogsProvider

    stop_state = SimpleNamespace(stopped=False)
    stop_state.is_set = lambda: stop_state.stopped
    stop_state.wait = lambda _timeout: stop_state.stopped
    calls = 0
    manager = SimpleNamespace(run=False)
    opened = []

    def on_updates(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            stop_state.stopped = True
        if manager.run:
            raise RuntimeError("Updates listener already running")
        manager.run = True
        if calls <= 2:
            raise ConnectionError("command registration failed")
        opened.append(True)

    def stop():
        manager.run = False

    provider = DialogsProvider(_dialogs_settings())
    provider._bot = SimpleNamespace(updates=SimpleNamespace(on_updates=on_updates, stop=stop))
    provider._updates_stop_event = stop_state
    caplog.set_level(logging.INFO)

    provider._updates_loop()

    assert opened == [True]
    assert caplog.text.count("event=dialogs_transport_unavailable") == 1
    assert caplog.text.count("event=dialogs_transport_recovery_started") == 1
    assert caplog.text.count("event=dialogs_transport_restored") == 1
    assert "Dialogs provider updates subscription active" not in caplog.text


def test_dialogs_updates_shutdown_interrupts_reconnect_wait(monkeypatch):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings())
    waiting = threading.Event()
    calls = []
    wait = provider._updates_stop_event.wait

    def wait_for_retry(timeout):
        waiting.set()
        return wait(timeout)

    def on_updates(**_kwargs):
        calls.append(True)
        raise ConnectionError("command registration failed")

    provider._bot = SimpleNamespace(updates=SimpleNamespace(on_updates=on_updates, stop=lambda: None))
    monkeypatch.setattr(provider._updates_stop_event, "wait", wait_for_retry)
    provider._ensure_updates_thread()
    try:
        assert waiting.wait(2.0)
    finally:
        provider.close()
    assert not provider._updates_thread.is_alive()
    assert calls == [True]


@pytest.mark.parametrize("source", ["dialogs", "sberchat"])
def test_dialogs_transport_claims_both_of_its_source_names(tmp_path, source):
    provider = _DummyProvider()
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="hello", source=source, markdown=True))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.text_calls == [("peer-42", "hello", "markdown", {})]
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_transport_drops_a_message_addressed_to_another_transport(tmp_path):
    """Not mine is acknowledged and dropped; a nack would stall the queue."""
    provider = _DummyProvider()
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(chat_id=1, text="browser reply", source="browser"))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.text_calls == []
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_transport_drops_a_chat_it_cannot_reach(tmp_path):
    provider = _DummyProvider()
    provider.can_send_to_chat_id = lambda _chat_id: False
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="unreachable"))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.text_calls == []
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_transport_retries_a_failed_delivery_then_gives_up(tmp_path, monkeypatch):
    """A transient failure is nacked; an endless one is dropped, not stalled."""
    from transport.dialogs import transport as mod

    monkeypatch.setattr(mod, "_DELIVERY_RETRY_BACKOFF_SEC", 0.0)
    provider = _DummyProvider()
    provider.send_text = lambda *_a, **_k: (False, "dialogs unavailable")
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="reply"))

    for attempts in range(mod._DELIVERY_ATTEMPT_LIMIT):
        transport.handle_outbound(settled.reader, replace(settled.delivered, attempts=attempts))

    assert settled.nacked == mod._DELIVERY_ATTEMPT_LIMIT - 1
    assert settled.acked == 1


def test_dialogs_provider_preserves_zero_chat_id():
    provider = _sdk_provider(SimpleNamespace())
    provider.settings = _dialogs_settings(DIALOGS_GROUP_ID="0")
    peer = SimpleNamespace(id=0)
    provider._remember_out_peer("0", peer)

    assert provider._group_id() == 0
    assert provider._resolve_out_peer(0) is peer


def _stored_outbound_attachment(tmp_path, *, data: bytes, filename: str, mime: str):
    from ouroboros.storage.blob.factory import get_storage_provider
    from transport.interfaces import AttachmentRef

    metadata = stored_file(
        scope=TEST_SCOPE,
        provider=get_storage_provider(),
        data=data,
        attachment_id=uuid.uuid4().hex,
        filename=filename,
        mime=mime,
        source="dialogs",
    )
    return AttachmentRef(
        attachment_id=str(metadata["id"]),
        filename=filename,
        mime=mime,
        size=len(data),
    )


def test_dialogs_transport_resolves_media_by_reference_at_delivery(tmp_path):
    from transport.interfaces import OutboundKind

    sent = []

    def send(peer, path, **kwargs):
        sent.append((Path(path).read_bytes(), kwargs["name"], kwargs["text"]))
        return _SdkUUID(1, 2)

    provider = _sdk_provider(SimpleNamespace(send_image_sync=send, send_file_sync=send))
    transport = _dialogs_transport(tmp_path, provider)
    transport.provider = provider
    image = _stored_outbound_attachment(tmp_path, data=b"png", filename="result.png", mime="image/png")
    document = _stored_outbound_attachment(tmp_path, data=b"pdf", filename="report.pdf", mime="application/pdf")
    try:
        for kind, reference in ((OutboundKind.PHOTO, image), (OutboundKind.FILE, document)):
            settled = _Delivery(_outbound(kind=kind, caption="caption", attachment=reference))
            transport.handle_outbound(settled.reader, settled.delivered)
            assert (settled.acked, settled.nacked) == (1, 0)
        assert sent == [(b"png", "result.png", "caption"), (b"pdf", "report.pdf", "caption")]
    finally:
        transport.close()


def test_dialogs_transport_drops_media_over_the_current_limit_without_retry(isolate_settings, tmp_path):
    from transport.interfaces import OutboundKind

    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 16})
    reference = _stored_outbound_attachment(tmp_path, data=b"document-data", filename="report.txt", mime="text/plain")
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 4})
    sent = []
    provider = _sdk_provider(SimpleNamespace(send_file_sync=lambda *a, **kw: sent.append(a)))
    with recording_blob_double() as recorder:
        transport = _dialogs_transport(tmp_path, provider)
        transport.provider = provider
        oversized = _Delivery(_outbound(kind=OutboundKind.FILE, attachment=reference))
        try:
            transport.handle_outbound(oversized.reader, oversized.delivered)
            assert (oversized.acked, oversized.nacked) == (1, 0)
            assert sent == []
            assert recorder.unbounded_reads == []
        finally:
            transport.close()


def test_dialogs_transport_passes_opt_in_reply_metadata_to_files(tmp_path):
    from transport.interfaces import OutboundKind

    provider = _DummyProvider()
    transport = _dialogs_transport(
        tmp_path,
        provider,
        DIALOGS_REPLY_ENABLED=True,
        DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED=True,
    )
    reference = _stored_outbound_attachment(tmp_path, data=b"pdf", filename="report.pdf", mime="application/pdf")
    settled = _Delivery(
        _outbound(
            kind=OutboundKind.FILE,
            attachment=reference,
            transport={"source_message_id": "mid-1", "target_uid": "user-1"},
        )
    )

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.file_calls[0][2]["reply_to"] == ["mid-1"]
    assert provider.file_calls[0][2]["target_uid"] == "user-1"


def test_dialogs_provider_forwarding_is_allowed_by_default():
    calls = []
    provider = _sdk_provider(SimpleNamespace(send_file_sync=lambda *args, **kwargs: calls.append((args, kwargs))))

    provider.send_file(
        "peer-42",
        _stored_media(b"report", "report.pdf"),
        scope=scope_for("dialogs", "peer-42"),
        filename="report.pdf",
        mime="application/pdf",
    )

    assert calls
    assert calls[0][1].get("is_forward_banned") in (None, False)


def test_dialogs_provider_retries_file_send_and_removes_temporary_path():
    paths = []

    def send_file(_peer, file_path, **_kwargs):
        paths.append(Path(file_path))
        assert Path(file_path).read_bytes() == b"report"
        if len(paths) == 1:
            raise OSError("temporary send failure")
        return SimpleNamespace(mid="file-mid")

    provider = _sdk_provider(SimpleNamespace(send_file_sync=send_file))

    result = provider.send_file(
        "peer-42", _stored_media(b"report", "report.pdf"), scope=scope_for("dialogs", "peer-42"), filename="report.pdf"
    )

    assert result == (True, "file-mid")
    assert len(paths) == 2
    assert not paths[-1].exists()


def test_dialogs_provider_does_not_retry_ambiguous_file_timeout():
    from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN

    calls = []

    def send_file(_peer, file_path, **_kwargs):
        calls.append(Path(file_path))
        raise TimeoutError("request timeout")

    provider = _sdk_provider(SimpleNamespace(send_file_sync=send_file))

    result = provider.send_file(
        "peer-42", _stored_media(b"report", "report.pdf"), scope=scope_for("dialogs", "peer-42"), filename="report.pdf"
    )

    assert result == (False, DELIVERY_UNKNOWN)
    assert len(calls) == 1
    assert not calls[0].exists()


def test_dialogs_file_send_logs_sdk_attempt_error_and_acceptance(caplog):
    calls = []

    def send_file(_peer, file_path, **_kwargs):
        calls.append(Path(file_path))
        if len(calls) == 1:
            raise ConnectionResetError(104, '{"access_hash":"super-secret-value","content":"private report body"}')
        return SimpleNamespace(message_id="mid-42")

    provider = _sdk_provider(SimpleNamespace(send_file_sync=send_file))

    with caplog.at_level(logging.INFO, logger="transport.dialogs.outbound"):
        result = provider.send_file(
            "peer-42",
            _stored_media(b"private report body", "report.txt"),
            scope=scope_for("dialogs", "peer-42"),
            filename="report.txt",
            task_id="task-1",
        )

    assert result == (True, "mid-42")
    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=dialogs_file_send_started") == 2
    assert operational.count("event=dialogs_file_send_failed") == 1
    assert operational.count("event=dialogs_file_send_accepted") == 1
    assert all(
        field in operational
        for field in (
            "task_id=task-1",
            "filename=report.txt",
            "stage=dialogs_send_file_sync",
            "attempt=1",
            "exception_type=ConnectionResetError",
            "duration=",
            "response_id=mid-42",
        )
    )
    assert "super-secret-value" not in operational
    assert "private report body" not in operational


def test_dialogs_image_send_error_log_redacts_sdk_exception(caplog):
    def fail(_peer, _path, **_kwargs):
        raise ConnectionResetError("access_hash=super-secret image-body")

    provider = _sdk_provider(SimpleNamespace(send_image_sync=fail))

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.outbound"):
        result = provider.send_image(
            "peer-42",
            _stored_media(b"image-body", "image.png"),
            scope=scope_for("dialogs", "peer-42"),
            filename="image.png",
        )

    assert result[0] is False
    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=dialogs_image_send_failed") == 2
    assert "stage=dialogs_send_image_sync attempt=1 exception_type=ConnectionResetError" in operational
    assert "message=sdk_call_failed duration=" in operational
    assert "super-secret" not in operational
    assert "image-body" not in operational


@parametrize_from_dict({"text": {"kind": "text"}, "file": {"kind": "file"}})
def test_dialogs_provider_times_out_sync_delivery_and_defers_media_cleanup(
    monkeypatch,
    kind,
):
    started = threading.Event()
    finish = threading.Event()
    paths = []

    def blocked(*args, **_kwargs):
        if kind == "file":
            paths.append(Path(args[1]))
        started.set()
        finish.wait(5.0)
        return SimpleNamespace(mid="mid-1")

    method = "send_message_sync" if kind == "text" else "send_file_sync"
    provider = _sdk_provider(SimpleNamespace(**{method: blocked}))
    monkeypatch.setattr(
        provider_module,
        "_DIALOGS_OUTBOUND_TIMEOUT_SEC",
        1.0,
        raising=False,
    )

    try:
        result = (
            provider.send_text("peer-42", "hello")
            if kind == "text"
            else provider.send_file(
                "peer-42",
                _stored_media(b"report", "report.txt"),
                scope=scope_for("dialogs", "peer-42"),
                filename="report.txt",
            )
        )
        assert started.wait(5)
        assert result[0] is False
        if kind == "file":
            assert result[1] == DELIVERY_UNKNOWN
        else:
            assert "timed out" in result[1]
        if paths:
            assert paths[0].exists()
    finally:
        finish.set()
        provider.close()
    if paths:
        assert not paths[0].exists()


def test_dialogs_provider_restores_sdk_reply_uuid_and_numeric_uid(monkeypatch):
    _install_sdk_uuid(monkeypatch)
    replies = []
    files = []

    def reply(peer, message_ids, text, **kwargs):
        replies.append((peer, message_ids, text, kwargs))
        return SimpleNamespace(mid=_SdkUUID(33, 44))

    def send_file(peer, path, **kwargs):
        assert Path(path).read_bytes() == b"report"
        files.append((peer, kwargs))
        return SimpleNamespace(mid=_SdkUUID(55, 66))

    provider = _sdk_provider(
        SimpleNamespace(
            reply_sync=reply,
            send_file_sync=send_file,
        ),
        DIALOGS_REPLY_ENABLED=True,
        DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED=True,
    )

    text_result = provider.send_text("peer-42", "answer", reply_to=["11:22"], target_uid="42")
    file_result = provider.send_file(
        "peer-42",
        _stored_media(b"report", "report.pdf"),
        scope=scope_for("dialogs", "peer-42"),
        filename="report.pdf",
        reply_to=["11:22"],
        target_uid="42",
    )

    assert text_result == (True, "33:44")
    assert file_result == (True, "55:66")
    for message_ids, kwargs in ((replies[0][1], replies[0][3]), (files[0][1]["reply"], files[0][1])):
        assert isinstance(message_ids[0], _SdkUUID)
        assert (message_ids[0].msb, message_ids[0].lsb) == (11, 22)
        assert kwargs["uid"] == 42


def test_dialogs_provider_does_not_treat_async_send_as_sync_success():
    provider = _sdk_provider(SimpleNamespace(send_message=lambda *_a, **_k: object()))

    assert provider.send_text("peer-42", "answer") == (
        False,
        "dialogs sdk messaging send method is not available",
    )


def test_dialogs_provider_malformed_reply_falls_back_to_regular_delivery():
    text_calls = []
    file_calls = []
    provider = _sdk_provider(
        SimpleNamespace(
            reply_sync=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no reply")),
            send_message_sync=lambda *args, **kwargs: text_calls.append((args, kwargs)),
            send_file_sync=lambda *args, **kwargs: file_calls.append((args, kwargs)),
        ),
        DIALOGS_REPLY_ENABLED=True,
    )

    assert provider.send_text("peer-42", "answer", reply_to=["legacy-id"])[0] is True
    assert (
        provider.send_file(
            "peer-42",
            _stored_media(b"report", "report.pdf"),
            scope=scope_for("dialogs", "peer-42"),
            filename="report.pdf",
            reply_to=["legacy-id"],
        )[0]
        is True
    )
    assert text_calls and file_calls
    assert "reply" not in file_calls[0][1]


def test_dialogs_provider_anchors_progress_update_on_fetched_message():
    sent_message = SimpleNamespace(mid="progress-mid")
    anchor = SimpleNamespace(mid="progress-mid")
    sends = []
    fetches = []
    updates = []
    provider = _sdk_provider(
        SimpleNamespace(
            send_message_sync=lambda *args, **kwargs: sends.append((args, kwargs)) or sent_message,
            get_message_by_id_sync=lambda mid: fetches.append(mid) or anchor,
            update_message_sync=lambda *args, **kwargs: updates.append((args, kwargs)),
        ),
        DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED=True,
    )

    provider.send_text("peer-42", "step one", is_progress=True, task_id="task-1")
    provider.send_text("peer-42", "step two", is_progress=True, task_id="task-1")
    provider.send_text("peer-42", "done", task_id="task-1")

    assert len(sends) == 2
    assert fetches == ["progress-mid"]
    assert updates == [((anchor, "step two"), {})]
    assert provider._progress_messages == {}


def test_dialogs_progress_anchor_accepts_uuid_send_result():
    sent_mid = _SdkUUID(11, 22)
    anchor = SimpleNamespace(mid=sent_mid)
    fetches = []
    updates = []
    provider = _sdk_provider(
        SimpleNamespace(
            send_message_sync=lambda *_a, **_k: sent_mid,
            get_message_by_id_sync=lambda mid: fetches.append(mid) or anchor,
            update_message_sync=lambda *args, **kwargs: updates.append(args),
        ),
        DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED=True,
    )

    assert provider.send_text("peer-42", "step one", is_progress=True, task_id="task-1") == (True, "11:22")
    assert provider.send_text("peer-42", "step two", is_progress=True, task_id="task-1") == (True, "11:22")

    assert fetches == [sent_mid]
    assert updates == [(anchor, "step two")]


@pytest.mark.parametrize(
    ("enabled", "response", "can_update", "reason"),
    [
        (True, SimpleNamespace(), True, "send returned no message id response_type=SimpleNamespace"),
        (True, SimpleNamespace(mid="progress-mid"), False, "update_message_sync missing"),
        (False, SimpleNamespace(mid="progress-mid"), True, ""),
    ],
)
def test_dialogs_progress_fallback_stays_successful_and_logs_one_reason(caplog, enabled, response, can_update, reason):
    sends = []
    fetches = []
    updates = []
    messaging = SimpleNamespace(
        send_message_sync=lambda *args, **kwargs: sends.append(args) or response,
        get_message_by_id_sync=lambda mid: fetches.append(mid) or SimpleNamespace(mid=mid),
    )
    if can_update:
        messaging.update_message_sync = lambda *args, **kwargs: updates.append(args)
    provider = _sdk_provider(messaging, DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED=enabled)

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.outbound"):
        for text in ("step one", "step two", "step three"):
            assert provider.send_text("peer-42", text, is_progress=True, task_id="task-1")[0] is True

    assert len(sends) == 3
    assert not fetches and not updates
    reasons = [record.getMessage() for record in caplog.records]
    assert len(reasons) == (1 if reason else 0)
    assert not reason or reason in reasons[0]


def test_dialogs_provider_serializes_update_uuid_without_losing_type_information():
    provider = _sdk_provider(SimpleNamespace())
    update = _dialogs_text_update("hello", message_id="legacy-message-id", user_id="42")
    update.mid = _SdkUUID(11, 22)

    inbound, reason = provider._normalize_sdk_update_message(update)

    assert reason == "accepted_text"
    assert inbound is not None
    assert inbound.source_message_id == "11:22"


def test_dialogs_provider_history_recovery_is_bounded_and_opt_in():
    from transport.dialogs import DialogsProvider

    calls = []
    peer = SimpleNamespace(id="peer-42", type=1)
    history = [
        _dialogs_text_update(f"old message {index}", message_id=f"old-{index}", peer=peer) for index in range(30)
    ]
    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_HISTORY_RECOVERY_ENABLED=True,
            DIALOGS_HISTORY_RECOVERY_LIMIT=25,
        )
    )
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(
            load_message_history_sync=lambda *args, **kwargs: calls.append((args, kwargs)) or history
        ),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )

    provider._recover_peer_history(peer)
    inbound = provider._incoming.get_nowait()

    assert calls == [((peer,), {"date": 0, "limit": 25})]
    assert inbound is not None and inbound.text == "old message 0"
    assert inbound.history_only is True
    assert provider._incoming.qsize() == 24


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0)], ids=["oldest-first", "newest-first", "shuffled"])
def test_dialogs_history_import_is_chronological_whatever_order_the_sdk_returns(order):
    from transport.dialogs import DialogsProvider

    peer = SimpleNamespace(id="peer-42", type=1)
    items = []
    for index, (text, date) in enumerate((("самое старое", 100), ("среднее", 200), ("самое новое", 300))):
        update = _dialogs_text_update(text, message_id=f"old-{index}", peer=peer)
        update.date = date
        items.append(update)
    provider = DialogsProvider(_dialogs_settings(DIALOGS_HISTORY_RECOVERY_ENABLED=True))
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(load_message_history_sync=lambda *_a, **_k: [items[i] for i in order]),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )

    provider._recover_peer_history(peer)

    queued = [provider._incoming.get_nowait().text for _ in range(3)]
    assert queued == ["самое старое", "среднее", "самое новое"]


def test_dialogs_private_peer_history_is_imported_once_after_its_first_live_message():
    from transport.dialogs import DialogsProvider

    peer = SimpleNamespace(id="peer-42", type=1)
    calls = []
    provider = DialogsProvider(_dialogs_settings(DIALOGS_HISTORY_RECOVERY_ENABLED=True))
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(
            load_message_history_sync=lambda *_a, **_k: (
                calls.append(1) or [_dialogs_text_update("прошлое", message_id="old-1", peer=peer)]
            )
        ),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )

    provider._on_text_message(_dialogs_text_update("новое", message_id="mid-1", peer=peer))
    provider._on_text_message(_dialogs_text_update("и ещё", message_id="mid-2", peer=peer))

    assert len(calls) == 1
    assert [provider._incoming.get_nowait().text for _ in range(3)] == ["новое", "прошлое", "и ещё"]


def test_dialogs_failed_history_import_is_retried_after_cooldown():
    from transport.dialogs import DialogsProvider

    peer = SimpleNamespace(id="peer-42", type=1)
    calls = []

    def loader(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sdk down")
        return [_dialogs_text_update("прошлое", message_id="old-1", peer=peer)]

    provider = DialogsProvider(_dialogs_settings(DIALOGS_HISTORY_RECOVERY_ENABLED=True))
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(load_message_history_sync=loader),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )

    provider._on_text_message(_dialogs_text_update("новое", message_id="mid-1", peer=peer))
    provider._on_text_message(_dialogs_text_update("ещё", message_id="mid-2", peer=peer))
    assert len(calls) == 1  # the failure cools down instead of retrying per message

    provider._history_recovery_failed_at.clear()  # the cooldown elapses
    provider._on_text_message(_dialogs_text_update("и ещё", message_id="mid-3", peer=peer))
    provider._on_text_message(_dialogs_text_update("совсем ещё", message_id="mid-4", peer=peer))

    assert len(calls) == 2  # retried once, then the peer counts as recovered
    queued = [provider._incoming.get_nowait().text for _ in range(5)]
    assert queued == ["новое", "ещё", "и ещё", "прошлое", "совсем ещё"]


def test_dialogs_text_replay_is_left_to_durable_idempotency():
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings())
    for peer_id in ("peer-1", "peer-2"):
        assert provider._queue_inbound_once(
            InboundMessage(
                chat_id=peer_id,
                user_id="user-1",
                text="hello",
                source_message_id="peer-local-mid",
            )
        )

    assert provider._incoming.qsize() == 2
    assert not provider._queue_inbound_once(InboundMessage(chat_id="peer-1", user_id="user-1", text="missing id"))


def test_dialogs_live_message_waits_for_inbound_queue_capacity():
    import queue

    provider = _sdk_provider(SimpleNamespace())
    provider._incoming = queue.Queue(maxsize=1)
    provider._queue_inbound_once(
        InboundMessage(chat_id="peer-42", user_id="user-1", text="занято", source_message_id="mid-0")
    )

    callback = threading.Thread(
        target=provider._on_text_message,
        args=(_dialogs_text_update("live message", message_id="live-mid"),),
    )
    callback.start()
    assert provider._incoming.get(timeout=1.0).text == "занято"
    callback.join(timeout=1.0)

    assert not callback.is_alive()
    assert provider._incoming.get(timeout=1.0).text == "live message"


def test_dialogs_recovered_history_starts_no_agent_task_even_when_it_mentions_the_bot(tmp_path):
    """Replayed history is this transport catching up, not the user speaking."""
    from ouroboros.storage.chat_store import list_owner_messages

    transport = _dialogs_transport(tmp_path, _DummyProvider())

    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="@ouroboros_bot собери отчёт",
            source_message_id="old-mid",
            history_only=True,
        )
    )

    assert transport.gateway.submitted == []
    persisted = list_owner_messages(tmp_path, scope=_PEER_42)
    assert [item.content for item in persisted] == ["@ouroboros_bot собери отчёт"]
    assert persisted[0].metadata["history_only"] is True
    assert persisted[0].metadata["inbound_processed"] is True


def test_typing_stop_event_uses_presence_cancel_action():
    from supervisor import events

    calls = []
    ctx = SimpleNamespace(
        send_typing=lambda *args, **kwargs: calls.append((args, kwargs)),
        bridge=SimpleNamespace(send_chat_action=lambda *_a, **_k: None),
    )

    events._handle_typing_stop(
        TypingStopped(scope=scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="0"), source="dialogs"), ctx
    )

    scope = scope_with(TEST_SCOPE, surface_id="dialogs", chat_id="0")
    assert calls == [((scope, "cancel"), {"source": "dialogs"})]


def test_dialogs_transport_reads_its_own_shared_subscription(tmp_path):
    """The reader loop takes what the supervisor published and delivers it."""
    from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox

    provider = _DummyProvider()
    gateway = InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox())
    transport = _dialogs_transport(tmp_path, provider, gateway=gateway)
    transport.start()
    try:
        gateway.outbox.publish(_outbound(text="browser reply", source="browser"))
        gateway.outbox.publish(_outbound(text="dialogs reply"))
        gateway.outbox.publish(_outbound(text="sberchat reply", source="sberchat"))
        deadline = time.monotonic() + 5.0
        while len(provider.text_calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        transport.close()

    assert [call[1] for call in provider.text_calls] == ["dialogs reply", "sberchat reply"]


class _ScriptedProvider(_DummyProvider):
    """Hands out a fixed list of updates, then goes quiet."""

    def __init__(self, updates):
        super().__init__()
        self.updates = list(updates)
        self.recv_calls = 0

    def recv_update(self, cursor, timeout_sec=0):
        self.recv_calls += 1
        if self.updates:
            return self.updates.pop(0)
        time.sleep(0.01)
        return None, cursor


def _pump(transport, *, until, timeout: float = 5.0) -> bool:
    """Run the transport's own loops until a condition holds, then stop them."""
    transport.start()
    try:
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            time.sleep(0.01)
        return until()
    finally:
        transport.close()


def test_dialogs_inbound_submits_with_its_string_chat_id_and_reply_metadata(tmp_path):
    """A peer id is not a number, and the reply route travels with the message."""
    transport = _dialogs_transport(tmp_path, _DummyProvider())

    transport.handle_inbound(
        InboundMessage(chat_id="peer-42", user_id="user-42", text="hello", source_message_id="mid-hello")
    )

    (envelope,) = transport.gateway.submitted
    assert envelope.chat_id == "peer-42"
    assert envelope.user_id == "user-42"
    assert envelope.source == "dialogs"
    assert envelope.client_message_id == "mid-hello"
    assert envelope.transport == {"source_message_id": "mid-hello", "target_uid": "user-42"}


def test_dialogs_input_loop_commits_the_cursor_and_releases_the_temp_file(tmp_path):
    cursors = sql_dialogs_cursor_repository()
    incoming_path = tmp_path / "incoming.pdf"
    incoming_path.write_bytes(b"pdf-data")
    provider = _ScriptedProvider([(_file_message(incoming_path, message_id="mid-15"), 15)])
    transport = _dialogs_transport(tmp_path, provider, cursor_repository=cursors)

    assert _pump(transport, until=lambda: bool(transport.gateway.submitted))
    assert cursors.load_cursor() == 15
    assert not incoming_path.exists()


def test_dialogs_input_loop_retries_a_pending_message_before_advancing_cursor(tmp_path):
    """A failure replays the same update; it never skips to the next one."""
    cursors = sql_dialogs_cursor_repository()
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()
    message = InboundMessage(chat_id="peer-42", user_id="user-42", text="retry me", source_message_id="retry-mid")
    provider = _ScriptedProvider([(message, 7)])
    transport = _dialogs_transport(tmp_path, provider, cursor_repository=cursors, gateway=gateway)
    attempts = []
    accept = gateway.inbox.submit

    def flaky(envelope):
        attempts.append(envelope)
        if len(attempts) == 1:
            raise OSError("temporary persistence failure")
        accept(envelope)

    gateway.inbox.submit = flaky

    assert _pump(transport, until=lambda: bool(gateway.submitted))
    assert len(attempts) == 2
    assert {envelope.client_message_id for envelope in attempts} == {"retry-mid"}
    assert provider.updates == []
    assert cursors.load_cursor() == 7


def test_dialogs_input_loop_holds_the_cursor_and_the_temp_file_while_submit_fails(tmp_path):
    """A message that was never handed over keeps its staged file and its offset."""
    cursors = sql_dialogs_cursor_repository()
    from tests.helpers.fake_gateway import FakeGateway

    incoming_path = tmp_path / "held.pdf"
    incoming_path.write_bytes(b"pdf-data")
    gateway = FakeGateway()
    attempts = []

    def refuse(envelope):
        attempts.append(envelope)
        raise OSError("inbox unavailable")

    gateway.inbox.submit = refuse
    provider = _ScriptedProvider([(_file_message(incoming_path, message_id="held-mid"), 9)])
    transport = _dialogs_transport(tmp_path, provider, cursor_repository=cursors, gateway=gateway)

    transport.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(attempts) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        # Still staged for the retry, and the offset has not moved past it.
        assert len(attempts) >= 2
        assert incoming_path.exists()
        assert cursors.load_cursor() == 0
    finally:
        transport.close()

    assert gateway.submitted == []
    assert provider.recv_calls == 1


def test_dialogs_inbound_persists_file_and_passes_only_attachment_metadata(tmp_path, caplog):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages

    incoming_path = tmp_path / "incoming.pdf"
    incoming_path.write_bytes(b"pdf-data")
    transport = _dialogs_transport(tmp_path, _DummyProvider())
    caplog.set_level(logging.INFO)

    transport.handle_inbound(_file_message(incoming_path, message_id="mid-16"))

    (envelope,) = transport.gateway.submitted
    assert len(envelope.attachment_ids) == 1
    assert envelope.image_base64 == ""
    assert "storage_key" in envelope.attachments[0]
    provider = get_storage_provider()
    assert provider.get_bytes(TEST_SCOPE, SCOPE_UPLOADS, envelope.attachments[0]["storage_key"]) == b"pdf-data"
    stored_message = list_owner_messages(tmp_path, scope=_PEER_42)[-1]
    assert stored_message.metadata["attachments"][0]["id"] == envelope.attachment_ids[0]
    assert stored_message.metadata["attachments"][0]["message_id"] == "mid-16"
    assert stored_message.metadata["inbound_processed"] is True
    assert "event=inbound_attachment_bound message_id=mid-16 filename=report.pdf size=8 source=dialogs" in caplog.text


def test_dialogs_inbound_keeps_file_and_cursor_for_retry_when_persistence_fails(monkeypatch, tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from transport.dialogs import transport as mod

    cursors = sql_dialogs_cursor_repository()
    cursors.save_cursor(16)
    transport = _dialogs_transport(tmp_path, _DummyProvider(), cursor_repository=cursors)
    incoming_path = tmp_path / "incoming.pdf"
    incoming_path.write_bytes(b"pdf-data")
    monkeypatch.setattr(
        mod,
        "append_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    with pytest.raises(OSError, match="database unavailable"):
        transport.handle_inbound(_file_message(incoming_path, message_id="mid-17"))

    provider = get_storage_provider()
    from sqlalchemy import text

    with runtime_persistence.current.engine.connect() as conn:
        rows = conn.execute(text("SELECT status, expires_at FROM files")).all()
    assert rows and all(status == "draft" and expires_at for status, expires_at in rows)
    assert provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "_meta/chat/") == []
    assert incoming_path.exists()
    assert cursors.load_cursor() == 16


def test_dialogs_inbound_attachment_replay_is_idempotent(tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages

    transport = _dialogs_transport(tmp_path, _DummyProvider())

    for attempt in (1, 2):
        incoming_path = tmp_path / f"incoming-{attempt}.pdf"
        incoming_path.write_bytes(b"pdf-data")
        transport.handle_inbound(_file_message(incoming_path, message_id="same-mid"))

    provider = get_storage_provider()
    assert len(transport.gateway.submitted) == 1
    assert len(list_owner_messages(tmp_path, scope=_PEER_42)) == 1
    assert len(provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")) == 1
    assert provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "_meta/chat/") == []


def test_dialogs_inbound_can_retry_after_a_failed_submit(tmp_path):
    """A failed submit leaves the record unprocessed; nothing is compensated."""
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()
    gateway.inbox.fail_with(OSError("task queue unavailable"))
    transport = _dialogs_transport(tmp_path, _DummyProvider(), gateway=gateway)
    incoming_path = tmp_path / "retry.pdf"
    incoming_path.write_bytes(b"pdf-data")
    message = _file_message(incoming_path, message_id="retry-mid")

    with pytest.raises(OSError, match="task queue unavailable"):
        transport.handle_inbound(message)

    provider = get_storage_provider()
    assert incoming_path.exists()
    pending_messages = list_owner_messages(tmp_path, scope=_PEER_42)
    assert len(pending_messages) == 1
    assert pending_messages[0].metadata["inbound_processed"] is False
    assert len(provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")) == 1

    gateway.inbox.fail_with(None)
    transport.handle_inbound(message)

    assert len(gateway.submitted) == 1
    assert len(list_owner_messages(tmp_path, scope=_PEER_42)) == 1
    assert list_owner_messages(tmp_path, scope=_PEER_42)[0].metadata["inbound_processed"] is True
    assert len(provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")) == 1


def test_dialogs_inbound_sweep_resubmits_what_was_never_handed_over(tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()
    gateway.inbox.fail_with(OSError("inbox unavailable"))
    transport = _dialogs_transport(tmp_path, _DummyProvider(), gateway=gateway)
    with pytest.raises(OSError):
        transport.handle_inbound(
            InboundMessage(chat_id="peer-42", user_id="user-42", text="swept", source_message_id="swept-mid")
        )

    gateway.inbox.fail_with(None)
    assert transport.resubmit_unprocessed() == 1

    (envelope,) = gateway.submitted
    assert envelope.text == "swept"
    assert envelope.client_message_id == "swept-mid"
    assert list_owner_messages(tmp_path, scope=_PEER_42)[0].metadata["inbound_processed"] is True
    assert transport.resubmit_unprocessed() == 0


def test_dialogs_input_loop_retries_after_a_cursor_commit_failure_without_duplicating(tmp_path):
    """The cursor is committed after the hand-over, so a failure replays a no-op."""
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages

    cursors = sql_dialogs_cursor_repository()
    cursors.save_cursor(40)
    incoming_path = tmp_path / "cursor-retry.pdf"
    incoming_path.write_bytes(b"pdf-data")
    provider = _ScriptedProvider([(_file_message(incoming_path, message_id="cursor-retry-mid"), 41)])
    transport = _dialogs_transport(tmp_path, provider, cursor_repository=cursors)
    commits = 0
    real_commit = transport.cursor.commit

    def flaky_commit(cursor):
        nonlocal commits
        commits += 1
        if commits == 1:
            raise OSError("cursor store unavailable")
        real_commit(cursor)

    transport.cursor.commit = flaky_commit

    assert _pump(transport, until=lambda: cursors.load_cursor() == 41)

    blobs = get_storage_provider()
    assert commits == 2
    assert len(transport.gateway.submitted) == 1
    assert not incoming_path.exists()
    assert len(list_owner_messages(tmp_path, scope=_PEER_42)) == 1
    assert len(blobs.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/")) == 1
    assert blobs.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "_meta/chat/") == []


def test_dialogs_input_loop_logs_full_failure_traceback(tmp_path, caplog):
    """The warning names the error and carries the traceback; the type alone hides the cause."""
    from tests.helpers.fake_gateway import FakeGateway

    gateway = FakeGateway()
    message = InboundMessage(chat_id="peer-42", user_id="user-42", text="boom", source_message_id="boom-mid")
    provider = _ScriptedProvider([(message, 3)])
    transport = _dialogs_transport(tmp_path, provider, gateway=gateway)
    attempts = []
    accept = gateway.inbox.submit

    def flaky(envelope):
        attempts.append(envelope)
        if len(attempts) == 1:
            raise ValueError("chat encryption key is not configured")
        accept(envelope)

    gateway.inbox.submit = flaky

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.transport"):
        assert _pump(transport, until=lambda: bool(gateway.submitted))

    failures = [record for record in caplog.records if "Dialogs input loop failed" in record.getMessage()]
    assert failures, caplog.text
    record = failures[0]
    assert "exception_type=ValueError" in record.getMessage()
    assert "error=chat encryption key is not configured" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is ValueError
    formatted = "".join(traceback.format_exception(*record.exc_info))
    assert "ValueError: chat encryption key is not configured" in formatted
    assert len(attempts) == 2


def test_dialogs_inbound_rolls_back_earlier_files_when_later_storage_fails(monkeypatch, tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from transport.dialogs import transport as mod

    transport = _dialogs_transport(tmp_path, _DummyProvider())
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    real_store = mod.store_attachment_fileobj
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("storage unavailable")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(mod, "store_attachment_fileobj", fail_second)
    with pytest.raises(OSError, match="storage unavailable"):
        transport.handle_inbound(
            InboundMessage(
                chat_id="peer-42",
                user_id="user-42",
                text="Два файла",
                source_message_id="two-files",
                attachments=(
                    InboundAttachment(
                        filename="first.txt",
                        mime="text/plain",
                        size=5,
                        temp_path=str(first_path),
                    ),
                    InboundAttachment(
                        filename="second.txt",
                        mime="text/plain",
                        size=6,
                        temp_path=str(second_path),
                    ),
                ),
            )
        )

    provider = get_storage_provider()
    from sqlalchemy import text

    with runtime_persistence.current.engine.connect() as conn:
        rows = conn.execute(text("SELECT status, expires_at FROM files")).all()
    assert rows and all(status == "draft" and expires_at for status, expires_at in rows)
    assert provider.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "_meta/chat/") == []


def test_dialogs_inbound_stages_attachments_under_the_arriving_operation(monkeypatch, tmp_path):
    """Staging the file is object-storage I/O, and it is the first thing the handler does.

    The input loop owns a thread that inherits no context, so a scope opened around the submit
    leaves every S3 request the upload makes — the metadata probe, the object, the two metadata
    writes — travelling with no `X-Trace-Id`, and the stored attachment cannot be followed back
    to the message that carried it.
    """
    from ouroboros.observability import aef_tracing
    from transport.dialogs import transport as mod

    transport = _dialogs_transport(tmp_path, _DummyProvider())
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    real_store = mod.store_attachment_fileobj
    observed: list[str] = []

    def record(*args, **kwargs):
        observed.append(aef_tracing.current_trace_id())
        return real_store(*args, **kwargs)

    monkeypatch.setattr(mod, "store_attachment_fileobj", record)
    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="Отчёт",
            trace_id=_TRACE_ID,
            source_message_id="staged-under-scope",
            attachments=(
                InboundAttachment(
                    filename="report.txt",
                    mime="text/plain",
                    size=6,
                    temp_path=str(path),
                ),
            ),
        )
    )

    assert observed == [_TRACE_ID]
    assert aef_tracing.current_trace_id() == ""


@pytest.mark.parametrize(
    "group_id,peer_id,peer_type",
    [
        pytest.param("peer-42", "other-peer", 2, id="different-group-id"),
        pytest.param("42", "42", 1, id="non-group-peer-type"),
    ],
)
def test_dialogs_provider_filters_configured_group(group_id, peer_id, peer_type):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_GROUP_ID=group_id,
        )
    )
    inbound, reason = provider._normalize_sdk_update_message(
        SimpleNamespace(
            peer=SimpleNamespace(id=peer_id, type=peer_type),
            sender_peer=SimpleNamespace(id="user-1"),
            message=SimpleNamespace(text_message=SimpleNamespace(text="hello")),
        )
    )

    assert inbound is None
    assert reason == "ignored_peer"


def test_dialogs_provider_filters_configured_user_id_inside_group(tmp_path):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_GROUP_ID="42",
            DIALOGS_USER_ID="user-1",
        )
    )
    inbound, reason = provider._normalize_sdk_update_message(
        SimpleNamespace(
            peer=SimpleNamespace(id="42", type=2),
            sender_peer=SimpleNamespace(id="user-2"),
            message=SimpleNamespace(text_message=SimpleNamespace(text="hello")),
        )
    )

    assert inbound is None
    assert reason == "ignored_user"


def test_dialogs_provider_accepts_configured_user_id_inside_group(tmp_path):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_GROUP_ID="42",
            DIALOGS_USER_ID="user-1",
        )
    )
    inbound, reason = provider._normalize_sdk_update_message(
        SimpleNamespace(
            peer=SimpleNamespace(id="42", type=2),
            sender_peer=SimpleNamespace(id="user-1"),
            message=SimpleNamespace(text_message=SimpleNamespace(text="hello")),
        )
    )

    assert reason == "accepted_group_text"
    assert inbound.chat_id == "42"
    assert inbound.user_id == "user-1"
    assert inbound.text == "hello"


def test_dialogs_provider_rejects_group_update_without_sender():
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(DIALOGS_GROUP_ID="42"))
    update = _dialogs_text_update("hello", peer=SimpleNamespace(id="42", type=2))
    update.sender_peer = None

    inbound, reason = provider._normalize_sdk_update_message(update)

    assert inbound is None
    assert reason == "missing_sender"


@pytest.mark.parametrize(
    "peer_id,peer_type,user_id,text",
    [
        pytest.param("peer-100", 2, "user-100", "hello", id="group-peer"),
        pytest.param("peer-200", 1, "user-200", "hello from dm", id="private-peer"),
    ],
)
def test_dialogs_provider_accepts_peers_when_group_filter_disabled(peer_id, peer_type, user_id, text):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        )
    )
    inbound, reason = provider._normalize_sdk_update_message(
        SimpleNamespace(
            peer=SimpleNamespace(id=peer_id, type=peer_type),
            sender_peer=SimpleNamespace(id=user_id),
            message=SimpleNamespace(text_message=SimpleNamespace(text=text)),
        )
    )

    assert reason == "accepted_text"
    assert inbound.chat_id == peer_id
    assert inbound.user_id == user_id
    assert inbound.text == text


def _identity_provider(get_user_by_id_sync=None, **settings):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(DIALOGS_GROUP_ID="42", **settings))
    provider._bot = SimpleNamespace(
        users=SimpleNamespace(get_user_by_id_sync=get_user_by_id_sync),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )
    return provider


def _group_update(text, *, user_id):
    return _dialogs_text_update(text, peer=SimpleNamespace(id="42", type=2), user_id=user_id)


def test_dialogs_group_senders_get_distinct_labels_resolved_once():
    lookups = []
    provider = _identity_provider(
        lambda uid: lookups.append(uid) or SimpleNamespace(data=SimpleNamespace(name=f"Сотрудник {uid}")),
        DIALOGS_PARTICIPANT_IDENTITY_ENABLED=True,
    )

    first, _reason = provider._normalize_sdk_update_message(_group_update("привет", user_id="1"))
    second, _reason = provider._normalize_sdk_update_message(_group_update("и я", user_id="2"))
    again, _reason = provider._normalize_sdk_update_message(_group_update("снова", user_id="1"))

    assert [first.sender_label, second.sender_label, again.sender_label] == [
        "Сотрудник 1",
        "Сотрудник 2",
        "Сотрудник 1",
    ]
    assert lookups == [1, 2]


def _failing_user_lookup(_uid):
    raise RuntimeError("sdk down")


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [
        pytest.param(_failing_user_lookup, "user-7", id="lookup-raises"),
        pytest.param(lambda _uid: SimpleNamespace(data=SimpleNamespace(name="")), "user-7", id="empty-name"),
        pytest.param(lambda _uid: None, "user-7", id="no-user"),
        pytest.param(
            lambda _uid: SimpleNamespace(data=SimpleNamespace(name="Ба[р]\nус")),
            "Ба р ус",
            id="unsafe-name",
        ),
    ],
)
def test_dialogs_sender_label_falls_back_per_sender_and_stays_safe(lookup, expected):
    calls = []
    provider = _identity_provider(
        lambda uid: calls.append(uid) or lookup(uid),
        DIALOGS_PARTICIPANT_IDENTITY_ENABLED=True,
    )

    first, _reason = provider._normalize_sdk_update_message(_group_update("hi", user_id="7"))
    second, _reason = provider._normalize_sdk_update_message(_group_update("hi again", user_id="7"))

    assert first.sender_label == expected
    assert second.sender_label == expected
    assert len(calls) == 1


def test_dialogs_sender_label_failure_is_retried_after_cooldown():
    calls = []

    def lookup(uid):
        calls.append(uid)
        if len(calls) == 1:
            raise RuntimeError("sdk down")
        return SimpleNamespace(data=SimpleNamespace(name="Сотрудник 7"))

    provider = _identity_provider(lookup, DIALOGS_PARTICIPANT_IDENTITY_ENABLED=True)

    first, _reason = provider._normalize_sdk_update_message(_group_update("hi", user_id="7"))
    second, _reason = provider._normalize_sdk_update_message(_group_update("hi again", user_id="7"))
    assert (first.sender_label, second.sender_label) == ("user-7", "user-7")
    assert len(calls) == 1  # the failure cools down instead of retrying per message

    provider._sender_label_retry_at.clear()  # the cooldown elapses
    third, _reason = provider._normalize_sdk_update_message(_group_update("hi third", user_id="7"))

    assert third.sender_label == "Сотрудник 7"
    assert len(calls) == 2


def test_dialogs_participant_identity_is_off_by_default():
    calls = []
    provider = _identity_provider(lambda uid: calls.append(uid))

    inbound, _reason = provider._normalize_sdk_update_message(_group_update("hi", user_id="7"))

    assert inbound.sender_label == ""
    assert calls == []


def test_dialogs_inbound_carries_the_sdk_message_date():
    provider = _sdk_provider(SimpleNamespace())
    update = _dialogs_text_update("привет")
    update.date = 1_700_000_000_000  # SDK dates are unix milliseconds

    inbound, _reason = provider._normalize_sdk_update_message(update)

    assert inbound.ts == "2023-11-14T22:13:20+00:00"


def test_dialogs_chat_id_identifies_the_chat():
    """The chat id is the axis a scope is minted from; there is no second spelling of it."""
    group_inbound, _reason = _identity_provider()._normalize_sdk_update_message(_group_update("hi", user_id="7"))
    private_inbound, _reason = _sdk_provider(SimpleNamespace())._normalize_sdk_update_message(
        _dialogs_text_update("hi", user_id="7")
    )

    assert str(group_inbound.chat_id) == "42"
    assert str(private_inbound.chat_id) == "peer-42"


def test_dispatch_persists_sender_label_and_the_owning_scope(tmp_path):
    """The label travels to the supervisor and into the row, or a group chat renders as
    `[User]`; the row itself is owned by the scope the transport minted."""
    from ouroboros.storage.chat_store import list_owner_messages

    transport = _dialogs_transport(tmp_path, _DummyProvider())
    transport.handle_inbound(
        InboundMessage(
            chat_id="42",
            user_id="7",
            text="привет",
            source_message_id="mid-1",
            sender_label="Сотрудник 7",
        )
    )

    (envelope,) = transport.gateway.submitted
    assert envelope.sender_label == "Сотрудник 7"
    assert (envelope.scope.surface_id, envelope.scope.chat_id) == (SurfaceId.DIALOGS, "42")
    persisted = list_owner_messages(tmp_path, scope=envelope.scope)[0]
    assert (persisted.owner_id, persisted.agent_id, persisted.chat_id) == (
        envelope.scope.owner_id,
        envelope.scope.agent_id,
        envelope.scope.chat_id,
    )
    assert persisted.metadata["sender_label"] == "Сотрудник 7"


@pytest.mark.parametrize(
    ("nick", "text", "expected", "starts_task"),
    [
        pytest.param("", "посмотри логи", "посмотри логи", True, id="gate-disabled"),
        pytest.param("ouroboros_bot", "посмотри логи", "посмотри логи", False, id="plain-chatter"),
        pytest.param("ouroboros_bot", "@ouroboros_bot  посмотри логи", "посмотри логи", True, id="mention-stripped"),
        pytest.param("@ouroboros_bot", "@OUROBOROS_BOT /status", "/status", True, id="mention-with-command"),
        pytest.param(
            "ouroboros_bot", "@ouroboros_botanik привет", "@ouroboros_botanik привет", False, id="another-nick"
        ),
        pytest.param("ouroboros_bot", "@ouroboros_bot, посмотри логи", "посмотри логи", True, id="mention-comma"),
        pytest.param("ouroboros_bot", "@ouroboros_bot: посмотри логи", "посмотри логи", True, id="mention-colon"),
        pytest.param("ouroboros_bot", "посмотри @ouroboros_bot логи", "посмотри логи", True, id="mention-inline"),
        pytest.param("ouroboros_bot", "посмотри @ouroboros_bot, логи", "посмотри логи", True, id="inline-comma"),
        pytest.param("ouroboros_bot", "посмотри @ouroboros_bot. логи", "посмотри логи", True, id="inline-period"),
        pytest.param("ouroboros_bot", "посмотри @ouroboros_bot: логи", "посмотри логи", True, id="inline-colon"),
        pytest.param("ouroboros_bot", "посмотри @ouroboros_bot; логи", "посмотри логи", True, id="inline-semicolon"),
        pytest.param("ouroboros_bot", "посмотри логи @ouroboros_bot", "посмотри логи", True, id="mention-trailing"),
        pytest.param("ouroboros_bot", "/status", "/status", True, id="runtime-command"),
        pytest.param("ouroboros_bot", "/clean", "/clean", True, id="retired-command"),
        pytest.param("ouroboros_bot", "почисти историю", "почисти историю", False, id="plain-speech"),
        pytest.param(
            "ouroboros_bot", "@ouroboros_bot почисти историю", "почисти историю", True, id="plain-speech-addressed"
        ),
        pytest.param("ouroboros_bot", "/deploy prod", "/deploy prod", False, id="another-bots-command"),
        pytest.param("ouroboros_bot", "/confirm abababab да", "/confirm abababab да", True, id="confirm-command"),
        pytest.param("ouroboros_bot", "да", "да", False, id="bare-answer-stays-context"),
    ],
)
def test_dialogs_addressing_gate_decides_what_starts_a_task(nick, text, expected, starts_task):
    provider = _identity_provider(DIALOGS_BOT_NICK=nick)

    inbound, reason = provider._normalize_sdk_update_message(_group_update(text, user_id="7"))

    assert (inbound.text, inbound.history_only) == (expected, not starts_task)
    assert reason == ("accepted_group_text" if starts_task else "context_only")


def test_dialogs_unaddressed_group_file_is_remembered_by_name_without_downloading():
    provider = _download_provider(
        lambda *_a, **_k: pytest.fail("an unaddressed file must not be downloaded"),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )

    provider._on_file_passed(_dialogs_file_update(peer_type=2))
    provider._file_jobs.join()

    inbound = provider._incoming.get_nowait()
    assert (inbound.text, inbound.history_only, inbound.attachments) == ("Вложение: report.txt", True, ())


def test_dialogs_unaddressed_oversized_file_stays_context_and_gets_no_reply():
    provider = _download_provider(
        lambda *_a, **_k: pytest.fail("an unaddressed file must not be downloaded"),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )
    provider.send_text = lambda *_a, **_k: pytest.fail("an unaddressed file must not be answered")

    provider._on_file_passed(_dialogs_file_update(peer_type=2, size=10**9))

    inbound = provider._incoming.get_nowait()
    assert (inbound.text, inbound.history_only, inbound.attachments) == ("Вложение: report.txt", True, ())


def test_dialogs_unaddressed_blocked_file_stays_context_and_gets_no_reply():
    provider = _download_provider(lambda *_a, **_k: None, DIALOGS_BOT_NICK="ouroboros_bot")
    provider.send_text = lambda *_a, **_k: pytest.fail("an unaddressed blocked file must not be answered")

    provider._on_file_blocked(_dialogs_file_update(peer_type=2))

    inbound = provider._incoming.get_nowait()
    assert (inbound.text, inbound.history_only) == ("Вложение: report.txt", True)


@parametrize_from_dict(
    {
        "text": {
            "messaging_method": "send_message_sync",
            "send": lambda provider: provider.send_text("peer-42", "мой ответ"),
        },
        "file": {
            "messaging_method": "send_file_sync",
            "send": lambda provider: provider.send_file(
                "peer-42",
                _stored_media(b"report body", "report.txt"),
                scope=scope_for("dialogs", "peer-42"),
                filename="report.txt",
            ),
        },
    }
)
def test_dialogs_reply_to_anything_the_bot_sent_starts_a_task(messaging_method, send):
    provider = _sdk_provider(
        SimpleNamespace(**{messaging_method: lambda *_a, **_k: _SdkUUID(11, 22)}),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )
    group_reply = _group_update("а тут что", user_id="7")
    group_reply.reply = [_SdkUUID(11, 22)]

    assert provider._normalize_sdk_update_message(_dialogs_text_update("посмотри логи"))[0].history_only is False
    assert provider._normalize_sdk_update_message(group_reply)[0].history_only is True

    send(provider)

    assert provider._normalize_sdk_update_message(group_reply)[0].history_only is False


def test_dialogs_scalar_reply_field_is_tolerated():
    provider = _sdk_provider(
        SimpleNamespace(send_message_sync=lambda *_a, **_k: _SdkUUID(11, 22)),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )
    provider.send_text("peer-42", "мой ответ")
    group_reply = _group_update("а тут что", user_id="7")
    group_reply.reply = _SdkUUID(11, 22)  # scalar instead of the declared list

    assert provider._normalize_sdk_update_message(group_reply)[0].history_only is False


def _bot_reply(msb, lsb):
    group_reply = _group_update("а тут что", user_id="7")
    group_reply.reply = [_SdkUUID(msb, lsb)]
    return group_reply


def _reply_lookup_provider(fetch):
    return _sdk_provider(
        SimpleNamespace(get_message_by_id_sync=fetch),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )


def test_dialogs_reply_to_forgotten_bot_message_is_resolved_via_server():
    # A restart, LRU eviction after 256 sends, a later chunk of a long answer,
    # a file the bot sent — all look the same here: an unremembered MID whose
    # author only the server knows.
    fetched = []
    provider = _reply_lookup_provider(
        lambda mid: fetched.append(mid) or SimpleNamespace(sender_peer=SimpleNamespace(id="bot"))
    )

    first = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]
    second = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]

    assert (first.history_only, second.history_only) == (False, False)
    # One lookup — the verdict lands in the sent-id LRU — and the SDK receives
    # the original UUID object, not its serialized form.
    assert [(type(mid), mid.msb, mid.lsb) for mid in fetched] == [(_SdkUUID, 11, 22)]


def test_dialogs_reply_to_another_participant_stays_context_and_is_cached():
    fetched = []
    provider = _reply_lookup_provider(
        lambda mid: fetched.append(str(mid)) or SimpleNamespace(sender_peer=SimpleNamespace(id="7"))
    )

    first = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]
    second = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]

    assert (first.history_only, second.history_only) == (True, True)
    assert fetched == ["11:22"]


def test_dialogs_reply_lookup_failure_degrades_to_context_and_is_retried():
    failures = []
    provider = _reply_lookup_provider(
        lambda mid: failures.append(str(mid)) or (_ for _ in ()).throw(RuntimeError("transport down"))
    )

    inbound, reason = provider._normalize_sdk_update_message(_bot_reply(11, 22))
    provider._normalize_sdk_update_message(_bot_reply(11, 22))

    assert (inbound.history_only, reason) == (True, "context_only")
    assert failures == ["11:22", "11:22"]  # a transient failure is not cached


def test_dialogs_reply_lookups_are_deduplicated_and_capped():
    fetched = []
    provider = _reply_lookup_provider(
        lambda mid: fetched.append(str(mid)) or SimpleNamespace(sender_peer=SimpleNamespace(id="7"))
    )
    group_reply = _group_update("а тут что", user_id="7")
    group_reply.reply = [_SdkUUID(1, lsb) for lsb in (1, 1, 2, 3, 4, 5)]

    assert provider._normalize_sdk_update_message(group_reply)[0].history_only is True
    assert fetched == ["1:1", "1:2"]


@pytest.mark.parametrize(
    "incomplete",
    [None, SimpleNamespace()],
    ids=["missing-message", "missing-sender"],
)
def test_dialogs_inconclusive_reply_lookup_is_not_cached_as_foreign(incomplete):
    responses = [incomplete, SimpleNamespace(sender_peer=SimpleNamespace(id="bot"))]
    provider = _reply_lookup_provider(lambda _mid: responses.pop(0))

    first = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]
    second = provider._normalize_sdk_update_message(_bot_reply(11, 22))[0]

    assert (first.history_only, second.history_only) == (True, False)


def test_dialogs_missing_reply_lookup_capability_warns_once(caplog):
    provider = _sdk_provider(SimpleNamespace(), DIALOGS_BOT_NICK="ouroboros_bot")

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.inbound"):
        provider._normalize_sdk_update_message(_bot_reply(11, 22))
        provider._normalize_sdk_update_message(_bot_reply(11, 22))

    warnings = [r for r in caplog.records if "get_message_by_id_sync is unavailable" in r.getMessage()]
    assert len(warnings) == 1


def test_dialogs_exhausted_lookup_slots_degrade_reply_detection_not_outbound():
    fetched = []
    provider = _sdk_provider(
        SimpleNamespace(
            get_message_by_id_sync=lambda mid: fetched.append(mid),
            send_message_sync=lambda *_a, **_k: _SdkUUID(11, 22),
        ),
        DIALOGS_BOT_NICK="ouroboros_bot",
    )
    provider._lookup_slots.acquire()
    provider._lookup_slots.acquire()  # both slots are held by hung lookups

    assert provider._normalize_sdk_update_message(_bot_reply(11, 22))[0].history_only is True
    assert fetched == []
    assert provider.send_text("peer-42", "мой ответ") == (True, "11:22")


def test_dialogs_reply_lookup_times_out_and_degrades(monkeypatch):
    from transport.dialogs import inbound as dialogs_inbound

    monkeypatch.setattr(dialogs_inbound, "_REPLY_LOOKUP_TIMEOUT_SEC", 0.05)
    provider = _reply_lookup_provider(lambda _mid: threading.Event().wait(0.5))

    assert provider._normalize_sdk_update_message(_bot_reply(11, 22))[0].history_only is True


def test_dialogs_history_recovery_does_not_resolve_replies_via_server():
    provider = _identity_provider(
        DIALOGS_BOT_NICK="ouroboros_bot",
        DIALOGS_HISTORY_RECOVERY_ENABLED=True,
    )
    peer = SimpleNamespace(id="42", type=2)
    update = _dialogs_text_update("а тут что", message_id="old-1", peer=peer, user_id="7")
    update.reply = [_SdkUUID(11, 22)]
    provider._bot.messaging = SimpleNamespace(
        load_message_history_sync=lambda *_a, **_k: [update],
        get_message_by_id_sync=lambda *_a: pytest.fail("recovered history must not resolve replies"),
    )

    provider._recover_peer_history(peer)

    assert provider._incoming.get_nowait().history_only is True


def test_dialogs_history_import_keeps_everything_and_starts_nothing():
    provider = _identity_provider(
        DIALOGS_BOT_NICK="ouroboros_bot",
        DIALOGS_HISTORY_RECOVERY_ENABLED=True,
    )
    peer = SimpleNamespace(id="42", type=2)
    history = [
        _dialogs_text_update("обсуждаем без бота", message_id="old-1", peer=peer, user_id="7"),
        _dialogs_text_update("@ouroboros_bot собери отчёт", message_id="old-2", peer=peer, user_id="8"),
    ]
    provider._bot.messaging = SimpleNamespace(load_message_history_sync=lambda *_a, **_k: history)

    provider._recover_peer_history(peer)

    queued = [provider._incoming.get_nowait() for _ in range(2)]
    assert [item.text for item in queued] == ["обсуждаем без бота", "собери отчёт"]
    assert all(item.history_only for item in queued)


def test_dialogs_provider_omits_root_certificates_when_unset():
    from transport.dialogs import DialogsProvider

    bot_config = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443",
            DIALOGS_BOT_TOKEN="token",
        )
    )._build_bot_config()

    assert "root_certificates" not in bot_config


def test_dialogs_provider_builds_bot_config_with_root_certificates(tmp_path):
    from transport.dialogs import DialogsProvider

    cert_path = tmp_path / "russian_trusted_bundle.pem"
    cert_path.write_text("bundle", encoding="utf-8")
    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_ROOT_CERTIFICATES=str(cert_path),
        )
    )
    bot_config = provider._build_bot_config()

    assert bot_config["endpoint"] == "dialogs.internal:443"
    assert bot_config["is_secure"] is True
    assert bot_config["root_certificates"] == str(cert_path)


def test_dialogs_provider_uses_explicit_secure_flag_not_endpoint_scheme(tmp_path):
    from transport.dialogs import DialogsProvider

    secure_bot_config = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="http://dialogs.internal:443",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_SECURE=True,
        )
    )._build_bot_config()
    insecure_bot_config = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_SECURE=False,
        )
    )._build_bot_config()

    assert secure_bot_config["endpoint"] == "dialogs.internal:443"
    assert secure_bot_config["is_secure"] is True
    assert insecure_bot_config["endpoint"] == "dialogs.internal:443"
    assert insecure_bot_config["is_secure"] is False


def test_dialogs_provider_uses_independent_file_url_port_for_insecure_endpoint():
    from transport.dialogs import DialogsProvider

    bot_config = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="http://epbotsift.sberchat.sberbank.ru:8080",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_SECURE=False,
            DIALOGS_FILE_URL_PORT=8081,
        )
    )._build_bot_config()

    assert bot_config["requests_url_replace"] == {
        "protocol": "http",
        "port": 8081,
    }


def test_dialogs_provider_registers_group_message_handler(tmp_path, monkeypatch):
    from transport.dialogs import DialogsProvider

    observed = {}

    class _MessageHandler:
        def __init__(self, callback, content_type, peer_type=None):
            observed["callback"] = callback
            observed["content_type"] = content_type
            observed["peer_type"] = peer_type

    fake_messaging_module = SimpleNamespace(
        MessageContentType=SimpleNamespace(TEXT_MESSAGE="text"),
        MessageHandler=_MessageHandler,
    )
    fake_peers_module = SimpleNamespace(PeerType=SimpleNamespace(PEERTYPE_GROUP=2))
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.messaging", fake_messaging_module)
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.peers", fake_peers_module)

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_GROUP_ID="42",
        )
    )
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(message_handler=lambda handlers: observed.setdefault("handlers", handlers)),
    )

    provider._register_handlers()

    assert observed["content_type"] == "text"
    assert observed["peer_type"] == 2
    assert len(observed["handlers"]) == 1


def test_dialogs_provider_registers_dlp_file_handlers(monkeypatch):
    from transport.dialogs import DialogsProvider

    observed = {}

    class _MessageHandler:
        def __init__(self, callback, content_type, peer_type=None):
            del callback, content_type, peer_type

    class _CustomUpdateHandler:
        def __init__(self, callback, update_type):
            self.callback = callback
            self.update_type = update_type

    monkeypatch.setitem(
        sys.modules,
        "dialog_bot_sdk.entities.messaging",
        SimpleNamespace(
            MessageContentType=SimpleNamespace(TEXT_MESSAGE="text"),
            MessageHandler=_MessageHandler,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "dialog_bot_sdk.entities.peers",
        SimpleNamespace(PeerType=SimpleNamespace(PEERTYPE_GROUP=2, PEERTYPE_PRIVATE=1)),
    )
    monkeypatch.setitem(
        sys.modules,
        "dialog_bot_sdk.entities.sequence_and_updates",
        SimpleNamespace(
            CustomUpdateHandler=_CustomUpdateHandler,
            CustomUpdateType=SimpleNamespace(UpdateFilePassed="passed", UpdateFileBlocked="blocked"),
        ),
    )

    provider = DialogsProvider(_dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpc://dialogs", DIALOGS_BOT_TOKEN="token"))
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(message_handler=lambda _handlers: None),
        updates=SimpleNamespace(custom_update_handler=lambda handlers: observed.setdefault("handlers", handlers)),
    )

    provider._register_handlers()

    assert [handler.update_type for handler in observed["handlers"]] == ["passed", "blocked"]


@pytest.mark.parametrize(
    "limit,label",
    [(12, "12 байт"), (1536, "1.5 КиБ (1536 байт)"), (1572864, "1.5 МиБ (1572864 байт)")],
)
def test_dialogs_provider_rejects_declared_oversize_before_download(isolate_settings, limit, label):
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": limit})

    downloads = []
    provider = _download_provider(lambda *args, **_kwargs: downloads.append(args))
    notices = []
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")
    update = _dialogs_file_update(
        message_id="mid-large",
        filename="large.pdf",
        mime="application/pdf",
        size=limit + 1,
    )

    provider._on_file_passed(update)

    assert downloads == []
    assert provider._incoming.empty()
    assert notices == [
        (
            "peer-42",
            f"Файл «large.pdf» отклонён: размер превышает {label}.",
            {"reply_to": ["mid-large"], "target_uid": "user-42"},
        )
    ]


def test_dialogs_provider_reports_actual_download_size_overflow(isolate_settings):
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 8})
    downloads = []

    def download_file(path, _document, **_kwargs):
        downloads.append(path)
        Path(path).write_bytes(b"123456789")

    notices = []
    provider = _download_provider(download_file)
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")

    try:
        provider._on_file_passed(_dialogs_file_update(filename="large.png", mime="image/png"))
        provider._file_jobs.join()

        assert len(downloads) == 1
        assert provider._incoming.empty()
        assert notices == [
            (
                "peer-42",
                "Файл «large.png» отклонён: размер превышает 8 байт.",
                {"reply_to": ["mid-1"], "target_uid": "user-42"},
            )
        ]
    finally:
        provider.close()


def test_dialogs_file_download_logs_safe_milestones_and_error(caplog, monkeypatch):
    from ouroboros.storage.attachment_validation import AttachmentError
    from transport.dialogs import inbound as inbound_module

    provider = _download_provider(lambda path, _document, **_k: Path(path).write_text("report", encoding="utf-8"))
    caplog.set_level(logging.INFO)

    provider._on_file_passed(_dialogs_file_update(message_id="mid-log", size=6))
    provider._file_jobs.join()

    assert "event=dialogs_file_received message_id=mid-log filename=report.txt" in caplog.text
    assert "declared_mime=text/plain declared_size=6 source=dialogs" in caplog.text
    assert "event=dialogs_file_download_started" in caplog.text
    assert "event=dialogs_file_download_completed" in caplog.text
    assert "actual_size=6" in caplog.text
    assert "event=dialogs_file_validation_passed" in caplog.text
    inbound = provider._incoming.get_nowait()
    Path(inbound.attachments[0].temp_path).unlink()

    def fail(_path, _document, **_kwargs):
        raise ConnectionResetError(104, '{"access_hash":"super-secret","content":"private report body"}')

    failing_provider = _download_provider(fail)
    update = _dialogs_file_update(message_id="mid-error", filename="../report.txt")
    with pytest.raises(ConnectionResetError):
        failing_provider._process_file_passed(update, attempt=2)

    assert "filename=report.txt message_id=mid-error attempt=2 stage=download_file_sync" in caplog.text
    assert "exception_type=ConnectionResetError" in caplog.text
    assert "super-secret" not in caplog.text
    assert "private report body" not in caplog.text
    assert "access_hash" not in caplog.text

    caplog.clear()
    provider.send_text = lambda *_args, **_kwargs: (True, "ok")

    def reject(*_args, **_kwargs):
        raise AttachmentError("invalid_attachment", "parser-secret\nraw path")

    monkeypatch.setattr(inbound_module, "validate_attachment_path", reject)
    provider._process_file_passed(_dialogs_file_update(message_id="mid-parser"))

    assert "error_code=invalid_attachment reason=validation_rejected" in caplog.text
    assert "parser-secret" not in caplog.text
    assert "raw path" not in caplog.text


def test_dialogs_missing_downloader_logs_download_failure(caplog):
    provider = _download_provider(lambda *_args, **_kwargs: None)
    provider._bot.internal.downloading.download_file_sync = None

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        provider._process_file_passed(_dialogs_file_update(message_id="mid-missing"), attempt=2)

    assert "event=dialogs_file_download_started filename=report.txt message_id=mid-missing attempt=2" in caplog.text
    assert "event=dialogs_file_download_failed filename=report.txt message_id=mid-missing attempt=2" in caplog.text
    assert "stage=download_file_sync exception_type=RuntimeError message=download_failed duration=" in caplog.text


def test_dialogs_download_setup_failure_releases_capacity(monkeypatch, tmp_path):
    from transport.dialogs import inbound as inbound_module

    provider = _download_provider(lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        inbound_module.os,
        "mkfifo",
        lambda *_args: (_ for _ in ()).throw(OSError("fifo unavailable")),
    )

    with pytest.raises(OSError, match="fifo unavailable"):
        provider._download_document(lambda *_args: None, tmp_path / "report.txt", object(), 1024, chat_id="peer-42")

    assert provider._download_slots.acquire(blocking=False)
    assert provider._download_slots.acquire(blocking=False)
    provider._download_slots.release()
    provider._download_slots.release()


@pytest.mark.parametrize(
    ("filename", "mime", "payload"),
    [
        ("archive.zip", "application/zip", b"PK\x03\x04data"),
        ("report.pdf", "application/pdf", b"not-a-pdf"),
        ("report.txt", "application/octet-stream", b"report"),
    ],
)
def test_dialogs_provider_rejects_invalid_file_and_names_it(filename, mime, payload):
    notices = []
    downloads = []

    def download(path, _document, **_kwargs):
        downloads.append(path)
        Path(path).write_bytes(payload)

    provider = _download_provider(download)
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")

    try:
        provider._on_file_passed(_dialogs_file_update(filename=filename, mime=mime))
        provider._file_jobs.join()

        assert len(downloads) == 1
        assert provider._incoming.empty()
        assert len(notices) == 1
        assert filename in notices[0][1]
    finally:
        provider.close()


def test_dialogs_shutdown_prevents_late_file_enqueue_and_removes_temp(monkeypatch):
    from transport.dialogs import inbound as provider_module

    started = threading.Event()
    finish = threading.Event()
    download_paths = []

    def download(path, _document, **_kwargs):
        download_paths.append(Path(path))
        Path(path).write_text("valid text", encoding="utf-8")

    def validate(path, *_args, **_kwargs):
        started.set()
        assert finish.wait(3.0)
        return SimpleNamespace(mime="text/plain", size=path.stat().st_size)

    provider = _download_provider(download)
    monkeypatch.setattr(provider_module, "validate_attachment_path", validate)
    provider._on_file_passed(_dialogs_file_update())
    assert started.wait(1.0)

    closer = threading.Thread(target=provider.close)
    closer.start()
    threading.Event().wait(1.1)
    assert closer.is_alive()
    finish.set()
    closer.join(timeout=2.0)

    final_path = download_paths[0].with_name(download_paths[0].name[1 : -len(".download")])
    assert not closer.is_alive()
    assert provider._incoming.empty()
    assert not final_path.exists()


def test_dialogs_provider_incoming_queue_is_bounded_and_close_removes_temp_files(tmp_path):
    from transport.dialogs import DialogsProvider

    temp_path = tmp_path / "queued.pdf"
    temp_path.write_bytes(b"pdf")
    provider = DialogsProvider(_dialogs_settings())
    provider._incoming.put_nowait(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="queued",
            attachments=(
                InboundAttachment(
                    filename="queued.pdf",
                    mime="application/pdf",
                    size=3,
                    temp_path=str(temp_path),
                ),
            ),
        )
    )

    assert provider._incoming.maxsize > 0
    provider.close()
    assert not temp_path.exists()


def test_dialogs_file_callback_does_not_block_on_download(monkeypatch):
    _stub_attachment_parser(monkeypatch)
    download_started = threading.Event()
    allow_download = threading.Event()

    def download_file(path, _document, **_kwargs):
        download_started.set()
        assert allow_download.wait(2.0)
        Path(path).write_text("report data", encoding="utf-8")

    provider = _download_provider(download_file)
    update = _dialogs_file_update(message_id="mid-async")
    callback = threading.Thread(target=provider._on_file_passed, args=(update,))

    try:
        callback.start()
        assert download_started.wait(1.0)
        callback.join(timeout=0.1)
        assert not callback.is_alive()
    finally:
        allow_download.set()
        callback.join(timeout=1.0)

    inbound = provider._incoming.get(timeout=2.0)
    Path(inbound.attachments[0].temp_path).unlink()


def test_dialogs_file_callback_applies_backpressure_when_job_queue_is_full(monkeypatch):
    import queue

    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings())
    provider._file_jobs = queue.Queue(maxsize=1)
    provider._file_jobs.put_nowait("existing-job")
    monkeypatch.setattr(provider, "_ensure_file_workers", lambda: None)
    update = _dialogs_file_update(message_id="mid-backpressure")

    callback = threading.Thread(target=provider._on_file_passed, args=(update,))
    callback.start()
    callback.join(timeout=0.1)
    assert callback.is_alive()

    assert provider._file_jobs.get_nowait() == "existing-job"
    callback.join(timeout=1.0)

    assert not callback.is_alive()
    # The queue carries the job, which is the update plus the operation it arrived under.
    assert provider._file_jobs.get_nowait().update is update


def test_dialogs_provider_close_stops_file_workers():
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpc://dialogs", DIALOGS_BOT_TOKEN="token"))
    provider._ensure_file_workers()
    workers = list(provider._file_workers)

    provider.close()

    assert workers
    assert all(not worker.is_alive() for worker in workers)


def test_dialogs_runtime_close_waits_for_input_and_rejects_late_message(tmp_path):
    """Close blocks on the in-flight poll, and what arrives after it is dropped."""
    entered = threading.Event()
    release = threading.Event()
    handled = []

    class Provider(_DummyProvider):
        def recv_update(self, _cursor, timeout_sec=0):
            entered.set()
            assert release.wait(2)
            return InboundMessage(chat_id="peer-42", user_id="user-42", text="late", source_message_id="late-mid"), 1

        def close(self):
            handled.append("closed")

    transport = _dialogs_transport(tmp_path, Provider())
    transport.start()
    assert entered.wait(1)

    closer = threading.Thread(target=transport.close)
    try:
        closer.start()
        closer.join(timeout=0.1)
        assert closer.is_alive()
    finally:
        release.set()
        closer.join(timeout=2)

    assert not closer.is_alive()
    assert handled == ["closed"]
    assert transport.gateway.submitted == []


def test_dialogs_file_workers_retry_when_parser_slot_is_busy(tmp_path, monkeypatch, isolate_settings):
    from ouroboros.storage import attachment_validation as attachments
    from ouroboros.storage.attachment_validation import AttachmentError

    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC": 0})
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    slot_held = threading.Event()
    release_slot = threading.Event()
    slot_released = threading.Event()
    failures = []
    downloads = []
    notices = []
    run_parser = attachments.run_parser

    def parse(path, mime, *, mode):
        if path.read_text(encoding="utf-8") == "first.txt":
            with attachments._PARSER_SLOT:
                slot_held.set()
                assert release_slot.wait(5)
            slot_released.set()
            return ""
        try:
            return run_parser(path, mime, mode=mode)
        except AttachmentError as exc:
            failures.append((exc.code, exc.retryable, path))
            release_slot.set()
            assert slot_released.wait(5)
            raise

    def download(path, document, **_kwargs):
        downloads.append(document.name)
        Path(path).write_text(document.name, encoding="utf-8")

    monkeypatch.setattr(attachments, "run_parser", parse)
    provider = _download_provider(download)
    monkeypatch.setattr(provider._updates_stop_event, "wait", lambda _timeout: False)
    provider.send_text = lambda *args, **kwargs: notices.append((args, kwargs)) or (True, "ok")
    second_update = _dialogs_file_update(message_id="mid-second", filename="second.txt")

    try:
        provider._on_file_passed(_dialogs_file_update(message_id="mid-first", filename="first.txt"))
        assert slot_held.wait(5)
        provider._on_file_passed(second_update)
        provider._on_file_passed(second_update)
        with provider._file_jobs.all_tasks_done:
            assert provider._file_jobs.all_tasks_done.wait_for(
                lambda: provider._file_jobs.unfinished_tasks == 0, timeout=FILE_PIPELINE_TIMEOUT
            )

        assert len(provider._file_workers) == 2
        assert len(failures) == 1
        code, retryable, failed_path = failures[0]
        assert (code, retryable) == ("attachment_parser_unavailable", True)
        assert not failed_path.exists()
        assert notices == []
        assert downloads == ["first.txt", "second.txt", "second.txt"]
        with provider._incoming.mutex:
            inbound = list(provider._incoming.queue)
        assert sorted(message.source_message_id for message in inbound) == ["mid-first", "mid-second"]
        assert {Path(message.attachments[0].temp_path) for message in inbound} == set(tmp_path.glob("dialogs-*"))
        for message in inbound:
            attachment = message.attachments[0]
            assert Path(attachment.temp_path).read_text(encoding="utf-8") == attachment.filename
    finally:
        release_slot.set()
        provider.close()
    assert list(tmp_path.glob("*dialogs-*")) == []


@pytest.mark.parametrize(
    "stage,failure",
    [
        ("download", "retryable"),
        ("validation", "retryable"),
        ("download", "io_error"),
        ("download", "invalid_attachment"),
        ("validation", "invalid_attachment"),
        ("download", "value_error"),
    ],
)
def test_dialogs_file_workers_retry_only_temporary_errors(stage, failure, tmp_path, monkeypatch, caplog):
    from ouroboros.storage.attachment_validation import AttachmentError
    from transport.dialogs import inbound as inbound_module

    caplog.set_level(logging.INFO, logger="transport.dialogs.inbound")
    retryable = failure in {"retryable", "io_error"}
    if failure == "value_error":
        error = ValueError("invalid download")
    elif failure == "io_error":
        error = OSError("private SDK payload")
    else:
        error = AttachmentError(
            "attachment_parser_unavailable" if retryable else failure,
            "parser failed",
            filename="report.txt",
            retryable=retryable,
        )
    downloads = []
    notices = []
    backoffs = []

    def download(path, _document, **_kwargs):
        downloads.append(path)
        Path(path).write_text("report", encoding="utf-8")
        if stage == "download":
            raise error

    def validate(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(inbound_module, "validate_attachment_path", validate)
    provider = _download_provider(download)
    monkeypatch.setattr(provider._updates_stop_event, "wait", lambda timeout: backoffs.append(timeout) or False)
    provider.send_text = lambda chat_id, text, **kwargs: notices.append(text) or (True, "ok")
    try:
        provider._on_file_passed(_dialogs_file_update())
        with provider._file_jobs.all_tasks_done:
            assert provider._file_jobs.all_tasks_done.wait_for(
                lambda: provider._file_jobs.unfinished_tasks == 0, timeout=5
            )
        assert len(downloads) == (inbound_module._DIALOGS_FILE_PROCESS_ATTEMPTS if retryable else 1)
        assert backoffs == ([0.25, 0.5] if retryable else [])
        assert provider._incoming.empty()
        assert list(tmp_path.glob("*dialogs-*")) == []
        if retryable:
            assert notices == ["Файл «report.txt» отклонён: could not process the file; please send it again."]
            terminal = [record for record in caplog.records if "exhausted retries" in record.getMessage()]
            assert len(terminal) == 1
            assert terminal[0].levelno == logging.WARNING
            message = terminal[0].getMessage()
            assert "attempts=3" in message
            assert f"exception_type={type(error).__name__}" in message
            if failure == "retryable":
                assert "code=attachment_parser_unavailable reason=parser failed filename=report.txt" in message
            assert "private SDK payload" not in caplog.text
        elif failure == "invalid_attachment":
            assert notices == ["Файл «report.txt» отклонён: parser failed."]
        else:
            assert notices == []
    finally:
        provider.close()


@pytest.mark.parametrize(
    "filename,mime,data",
    [("report.txt", "text/plain", b"report data"), ("image.webp", "", b"RIFF1234WEBP")],
)
def test_dialogs_provider_retries_download_and_resolves_canonical_mime(monkeypatch, filename, mime, data):
    import mimetypes

    monkeypatch.setattr(mimetypes, "guess_type", lambda *_args, **_kwargs: (None, None))
    _stub_attachment_parser(monkeypatch)
    attempts = 0

    def download_file(path, _document, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary download failure")
        Path(path).write_bytes(data)

    provider = _download_provider(download_file)
    notices = []
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")
    update = _dialogs_file_update(
        message_id="mid-retry",
        filename=filename,
        mime=mime,
    )
    update.message.document_message.caption = "Проверь отчет"

    try:
        provider._on_file_passed(update)
        with provider._file_jobs.all_tasks_done:
            assert provider._file_jobs.all_tasks_done.wait_for(
                lambda: provider._file_jobs.unfinished_tasks == 0, timeout=30
            )
        inbound = provider._incoming.get_nowait()

        assert attempts == 2
        assert notices == []
        assert inbound.text == "Проверь отчет"
        assert inbound.source_message_id == "mid-retry"
        assert inbound.attachments[0].filename == filename
        assert inbound.attachments[0].mime == (mime or "image/webp")
        assert Path(inbound.attachments[0].temp_path).read_bytes() == data
        Path(inbound.attachments[0].temp_path).unlink()
    finally:
        provider.close()


def test_dialogs_provider_rejects_dlp_blocked_file_without_queueing():
    from transport.dialogs import DialogsProvider

    notices = []
    provider = DialogsProvider(_dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpc://dialogs", DIALOGS_BOT_TOKEN="token"))
    provider._bot = SimpleNamespace(user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))))
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")
    update = SimpleNamespace(
        peer=SimpleNamespace(id="peer-42", type=1),
        sender_peer=SimpleNamespace(id="user-42"),
        message=SimpleNamespace(
            mid="mid-2",
            document_message=SimpleNamespace(name="unsafe.pdf", file_id=10, access_hash=20),
        ),
    )

    provider._on_file_blocked(update)

    assert provider._incoming.empty()
    assert notices == [
        (
            "peer-42",
            "Файл «unsafe.pdf» заблокирован политикой безопасности SberChat.",
            {"reply_to": ["mid-2"], "target_uid": "user-42"},
        )
    ]


def test_dialogs_provider_registers_group_and_private_handlers_without_group_id(tmp_path, monkeypatch):
    from transport.dialogs import DialogsProvider

    observed = {}

    class _MessageHandler:
        def __init__(self, callback, content_type, peer_type=None):
            observed.setdefault("peer_types", []).append(peer_type)
            observed["callback"] = callback
            observed["content_type"] = content_type

    fake_messaging_module = SimpleNamespace(
        MessageContentType=SimpleNamespace(TEXT_MESSAGE="text"),
        MessageHandler=_MessageHandler,
    )
    fake_peers_module = SimpleNamespace(
        PeerType=SimpleNamespace(
            PEERTYPE_GROUP=2,
            PEERTYPE_PRIVATE=1,
            PEERTYPE_ENCRYPTEDPRIVATE=3,
        )
    )
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.messaging", fake_messaging_module)
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.peers", fake_peers_module)

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        )
    )
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(message_handler=lambda handlers: observed.setdefault("handlers", handlers)),
    )

    provider._register_handlers()

    assert observed["content_type"] == "text"
    assert observed["peer_types"] == [2, 1, 3]
    assert len(observed["handlers"]) == 3


def test_dialogs_provider_builds_group_peer_when_sdk_lookup_is_unavailable(tmp_path, monkeypatch):
    from transport.dialogs import DialogsProvider

    class _Peer:
        def __init__(self, type=None, id=None):
            self.type = type
            self.id = id

    fake_peers_module = SimpleNamespace(
        PeerType=SimpleNamespace(PEERTYPE_GROUP=2),
        Peer=_Peer,
    )
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.peers", fake_peers_module)

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            DIALOGS_GROUP_ID="42",
        )
    )
    provider._bot = SimpleNamespace(groups=SimpleNamespace())

    peer = provider._resolve_group_peer()

    assert peer is not None
    assert peer.id == 42
    assert peer.type == 2
    assert provider._resolve_out_peer("42") is peer


def test_dialogs_provider_authenticate_skips_group_resolution_without_group_id(tmp_path, monkeypatch):
    from transport.dialogs import DialogsProvider

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        )
    )
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(message_handler=lambda _handlers: None),
    )

    monkeypatch.setattr(provider, "_ensure_bot", lambda: provider._bot)
    monkeypatch.setattr(provider, "_register_handlers", lambda: None)
    monkeypatch.setattr(provider, "_ensure_updates_thread", lambda: None)
    monkeypatch.setattr(
        provider,
        "_resolve_group_peer",
        lambda: (_ for _ in ()).throw(AssertionError("group resolution must stay disabled")),
    )

    provider.authenticate()


def test_dialogs_provider_does_not_build_group_peer_without_group_id(tmp_path, monkeypatch):
    from transport.dialogs import DialogsProvider

    class _Peer:
        def __init__(self, type=None, id=None):
            self.type = type
            self.id = id

    fake_peers_module = SimpleNamespace(
        PeerType=SimpleNamespace(PEERTYPE_GROUP=2),
        Peer=_Peer,
    )
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.peers", fake_peers_module)

    provider = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        )
    )
    provider._bot = SimpleNamespace(groups=SimpleNamespace())

    assert provider._resolve_group_peer() is None
    assert provider._resolve_out_peer("42") is None


def test_send_message_event_preserves_string_chat_id():
    from supervisor import events

    calls = []
    ctx = SimpleNamespace(
        send_with_budget=lambda *args, **kwargs: calls.append((args, kwargs)),
        DRIVE_ROOT=".",
    )

    events._handle_send_message(
        OutboundText(scope=_PEER_42, text="hello", task_id="task-1", source="dialogs"),
        ctx,
    )

    # The chat is read off the scope now; the first argument is the text.
    assert calls[0][0][0] == "hello"
    assert calls[0][1]["scope"].chat_id == "peer-42"


def test_dialogs_slash_command_is_submitted_and_answered_over_the_seam(tmp_path, monkeypatch):
    """The command travels the one inbound path, and the answer keeps its route."""
    from supervisor import commands
    from supervisor.orchestrator.intent import TaskIntentKind
    from supervisor.runtime import SupervisorLoop

    monkeypatch.setattr(commands, "status_text", lambda *_args, **_kwargs: "status")
    transport = _dialogs_transport(tmp_path, _DummyProvider())

    transport.handle_inbound(
        InboundMessage(chat_id="group-42", user_id="alice", text="/status", source_message_id="mid-1")
    )

    (envelope,) = transport.gateway.submitted
    assert envelope.transport == {"source_message_id": "mid-1", "target_uid": "alice"}

    answers = []
    from ouroboros.storage.runtime_models import OwnerRuntimeState

    # The real model, not a stand-in: a hand-rolled namespace drifts from the fields the
    # loop actually writes, and then this test stops exercising the claim path at all.
    state = OwnerRuntimeState()

    @contextlib.contextmanager
    def _mutate():
        yield state

    class _StateRepo(SimpleNamespace):
        def __init__(self) -> None:
            super().__init__(load=lambda: state, save=lambda _st: None, mutate=_mutate)

        def for_scope(self, _scope):
            return self

    ctx = SimpleNamespace(
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        DRIVE_ROOT=tmp_path,
        drive_root=tmp_path,
        # ``for_scope`` returns the same stand-in: the row is per owner now, and the loop
        # addresses the sender's rather than the instance's.
        state_repo=_StateRepo(),
        orchestrator=SimpleNamespace(
            submit=lambda _envelope: SimpleNamespace(intent=TaskIntentKind.SLASH_COMMAND, slash_command="/status")
        ),
        executor=SimpleNamespace(worker_slots_dict=dict),
        soft_timeout=600,
        hard_timeout=1800,
        send_with_budget=lambda *args, **kwargs: answers.append((args, kwargs)),
    )
    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *_a, **_k: None)

    SupervisorLoop(
        ctx=ctx, stop_event=threading.Event(), inbox=transport.gateway.inbox, heartbeat=supervisor_heartbeat()
    ).handle_inbound(envelope)

    assert answers[-1][1]["source"] == "dialogs"
    assert answers[-1][1]["transport"] == {"source_message_id": "mid-1", "target_uid": "alice"}


def test_dialogs_slash_retry_does_not_resubmit_a_processed_command(tmp_path):
    """The record is marked processed, so a replayed update hands over nothing."""
    transport = _dialogs_transport(tmp_path, _DummyProvider())
    message = InboundMessage(
        chat_id="group-42",
        user_id="alice",
        text="/status",
        source_message_id="mid-status-1",
    )

    transport.handle_inbound(message)
    transport.handle_inbound(message)

    assert len(transport.gateway.submitted) == 1


def test_local_chat_bridge_publishes_a_string_chat_id_unchanged():
    """A peer id is not a number, and coercing it would address the wrong chat."""
    import supervisor.message_bus as message_bus

    outbox = RecordingOutbox()
    bridge = message_bus.LocalChatBridge(outbox)

    ok, status = bridge.send_message("hello", scope=_PEER_42, source="dialogs")

    assert (ok, status) == (True, "ok")
    assert [message.chat_id for message in outbox.published] == ["peer-42"]


def test_local_chat_bridge_publishes_every_source_without_routing():
    """The bridge delivers nothing: it publishes, and a transport claims what is its."""
    import supervisor.message_bus as message_bus

    outbox = RecordingOutbox()
    bridge = message_bus.LocalChatBridge(outbox)

    bridge.send_message("dialogs reply", scope=TEST_SCOPE, source="dialogs")
    bridge.send_message("browser reply", scope=TEST_SCOPE, source="browser")

    assert [(message.source, message.text) for message in outbox.published] == [
        ("dialogs", "dialogs reply"),
        ("browser", "browser reply"),
    ]


def test_dialogs_command_is_submitted_like_any_message(tmp_path):
    """Recognizing the command is the supervisor's job; the transport hands it over."""
    from ouroboros.storage.chat_store import list_owner_messages

    transport = _dialogs_transport(tmp_path, _DummyProvider())

    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="/status",
            source_message_id="mid-status",
        )
    )

    (envelope,) = transport.gateway.submitted
    assert envelope.text == "/status"
    assert envelope.transport == {"source_message_id": "mid-status", "target_uid": "user-42"}
    persisted = list_owner_messages(tmp_path, scope=_PEER_42)
    assert [item.content for item in persisted] == ["/status"]
    assert persisted[0].metadata["inbound_processed"] is True


@pytest.mark.parametrize("command", ["/status", "/clean"], ids=["owned", "retired"])
def test_dialogs_command_with_attachment_is_rejected_atomically(tmp_path, command):
    """Nothing is submitted, the staged blobs go, and the user is told on the spot."""
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_attachments import get_attachment
    from ouroboros.storage.chat_store import list_owner_messages

    incoming_path = tmp_path / "note.txt"
    incoming_path.write_text("hello", encoding="utf-8")
    provider = _DummyProvider()
    transport = _dialogs_transport(tmp_path, provider)

    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text=command,
            source_message_id="mid-status",
            attachments=(
                InboundAttachment(
                    filename="note.txt",
                    mime="text/plain",
                    size=5,
                    temp_path=str(incoming_path),
                ),
            ),
        )
    )

    blobs = get_storage_provider()
    assert transport.gateway.submitted == []
    assert [call[1] for call in provider.text_calls] == ["Команды нельзя отправлять вместе с файлами: note.txt"]
    assert blobs.list_keys(TEST_SCOPE, SCOPE_UPLOADS, "chat/") == []
    assert get_attachment(TEST_SCOPE, "attachment-status") is None
    assert list_owner_messages(tmp_path, scope=_PEER_42) == []


def test_dialogs_history_only_command_is_persisted_without_execution(tmp_path):
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages

    transport = _dialogs_transport(tmp_path, _DummyProvider())
    append_chat_message(
        tmp_path,
        scope=_PEER_42,
        actor_type="owner",
        chat_id="peer-42",
        user_id=1,
        text="old",
        source="dialogs",
    )

    transport.handle_inbound(
        InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="/status",
            source_message_id="history-status",
            history_only=True,
        )
    )

    persisted = list_owner_messages(tmp_path, scope=_PEER_42)
    assert [item.content for item in persisted] == ["old", "/status"]
    assert persisted[-1].metadata["inbound_processed"] is True
    assert transport.gateway.submitted == []


# --- the operation an update belongs to --------------------------------------

#: A well-formed identifier in the one spelling this agent puts on the wire.
_TRACE_ID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def test_dialogs_text_message_originates_its_operation_where_the_update_arrives():
    """The SDK handler is the first frame that knows about this message; it is the origin."""
    from ouroboros.observability.aef_tracing import normalize_trace_id

    provider = _sdk_provider(SimpleNamespace())

    provider._on_text_message(_dialogs_text_update("привет", message_id="mid-trace-text"))

    inbound = provider._incoming.get(timeout=2.0)
    assert inbound.trace_id and normalize_trace_id(inbound.trace_id) == inbound.trace_id


def test_dialogs_file_leg_is_one_operation_from_arrival_to_submission():
    """Download and validation are the slow, failure-prone part; they belong to the operation."""
    from ouroboros.observability.aef_tracing import current_trace_id, normalize_trace_id

    while_downloading: list[str] = []

    def download_file(path, _document, **_kwargs):
        while_downloading.append(current_trace_id())
        Path(path).write_text("report data", encoding="utf-8")

    provider = _download_provider(download_file)

    provider._on_file_passed(_dialogs_file_update(message_id="mid-trace-file"))
    provider._file_jobs.join()

    inbound = provider._incoming.get(timeout=2.0)
    try:
        assert inbound.trace_id and normalize_trace_id(inbound.trace_id) == inbound.trace_id
        # One identifier for the whole leg: the worker thread continues the operation the
        # SDK handler started rather than running under none, or under a second one.
        assert while_downloading == [inbound.trace_id]
    finally:
        Path(inbound.attachments[0].temp_path).unlink()


def test_a_bounded_sdk_call_runs_in_its_callers_operation():
    """`_call_with_timeout` puts the SDK call on a thread so the wait can be bounded.

    A thread does not inherit context variables, so without the context copy every call
    routed through here — every send, the history import, the sender-label and reply
    lookups — ran under no operation at all, and so did the log records they write.
    """
    from ouroboros.observability import aef_tracing

    provider = _sdk_provider(SimpleNamespace())
    observed: list[str] = []

    with aef_tracing.trace_scope(_TRACE_ID):
        provider._call_with_timeout(
            lambda: observed.append(aef_tracing.current_trace_id()),
            operation="probe",
        )

    assert observed == [_TRACE_ID]


def test_a_bounded_sdk_call_outside_an_operation_stays_outside_one():
    """The copy carries what is active; it does not originate. A call with no caller —
    the startup history import — must not invent an identifier on a worker thread, where
    nothing would ever read it back."""
    from ouroboros.observability import aef_tracing

    provider = _sdk_provider(SimpleNamespace())
    observed: list[str] = []

    assert aef_tracing.current_trace_id() == ""
    provider._call_with_timeout(
        lambda: observed.append(aef_tracing.current_trace_id()),
        operation="probe",
    )

    assert observed == [""]


def test_dialogs_file_arrival_is_logged_inside_its_operation(caplog):
    """`event=dialogs_file_received` is the first line of the leg and must be joinable.

    Through the real filter, so this pins what an operator actually reads rather than
    what the scope happens to hold.
    """
    from ouroboros.observability.logging_setup import LogContextFilter

    provider = _download_provider(lambda path, _document, **_k: Path(path).write_text("d", encoding="utf-8"))
    caplog.handler.addFilter(LogContextFilter())

    try:
        with caplog.at_level(logging.INFO, logger="transport.dialogs.inbound"):
            provider._on_file_passed(_dialogs_file_update(message_id="mid-trace-log"))
            provider._file_jobs.join()
    finally:
        caplog.handler.filters = [f for f in caplog.handler.filters if not isinstance(f, LogContextFilter)]

    arrival = next(r for r in caplog.records if "dialogs_file_received" in r.getMessage())
    validated = next(r for r in caplog.records if "dialogs_file_validation_passed" in r.getMessage())
    inbound = provider._incoming.get(timeout=2.0)
    try:
        assert getattr(arrival, "trace_id", "") == inbound.trace_id
        assert getattr(validated, "trace_id", "") == inbound.trace_id
    finally:
        Path(inbound.attachments[0].temp_path).unlink()


def test_dialogs_send_text_carries_the_operation_as_grpc_metadata():
    """This transport's egress is a gRPC call, so the header rides its metadata.

    The bot outlives every operation, so the value is read per call: one fixed when the bot
    was built would attribute every message it ever sends to the first task.
    """
    from ouroboros.observability import aef_tracing

    sends: list[dict] = []
    provider = _sdk_provider(SimpleNamespace(send_message_sync=lambda *_a, **kwargs: sends.append(kwargs)))

    with aef_tracing.trace_scope(_TRACE_ID):
        assert provider.send_text("peer-42", "ответ")[0] is True

    assert sends == [{"metadata": {"x-trace-id": _TRACE_ID}}]


def test_dialogs_send_text_outside_an_operation_carries_no_metadata():
    """An absent header, not a generated one: an identifier nothing else names correlates nothing."""
    sends: list[dict] = []
    provider = _sdk_provider(SimpleNamespace(send_message_sync=lambda *_a, **kwargs: sends.append(kwargs)))

    assert provider.send_text("peer-42", "ответ")[0] is True

    assert sends == [{}]


def test_dialogs_reply_and_file_send_carry_the_operation_as_grpc_metadata(monkeypatch):
    """Every SDK method that puts something in the chat, not only the plain send."""
    from ouroboros.observability import aef_tracing

    _install_sdk_uuid(monkeypatch)
    replies: list[dict] = []
    files: list[dict] = []
    provider = _sdk_provider(
        SimpleNamespace(
            reply_sync=lambda *_a, **kwargs: replies.append(kwargs),
            send_file_sync=lambda *_a, **kwargs: files.append(kwargs),
        ),
        DIALOGS_REPLY_ENABLED=True,
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        assert provider.send_text("peer-42", "ответ", reply_to=["11:22"])[0] is True
        assert (
            provider.send_file(
                "peer-42",
                _stored_media(b"report", "report.pdf"),
                scope=scope_for("dialogs", "peer-42"),
                filename="report.pdf",
            )[0]
            is True
        )

    assert replies[0]["metadata"] == {"x-trace-id": _TRACE_ID}
    assert files[0]["metadata"] == {"x-trace-id": _TRACE_ID}


def test_dialogs_media_send_carries_the_operation_on_the_upload_transfer_too(monkeypatch):
    """A media send is two channels and only one of them was named.

    The SDK asks the server over gRPC and then puts the bytes on an HTTP transfer, exactly the
    split the download already follows — so the upload takes `headers=`, the canonical spelling,
    beside the gRPC `metadata=` the send itself carries.
    """
    from ouroboros.observability import aef_tracing

    _install_sdk_uuid(monkeypatch)
    files: list[dict] = []
    images: list[dict] = []
    provider = _sdk_provider(
        SimpleNamespace(
            send_file_sync=lambda *_a, **kwargs: files.append(kwargs),
            send_image_sync=lambda *_a, **kwargs: images.append(kwargs),
        )
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        assert (
            provider.send_file(
                "peer-42",
                _stored_media(b"report", "report.pdf"),
                scope=scope_for("dialogs", "peer-42"),
                filename="report.pdf",
            )[0]
            is True
        )
        assert (
            provider.send_image(
                "peer-42",
                _stored_media(b"png", "chart.png"),
                scope=scope_for("dialogs", "peer-42"),
                filename="chart.png",
            )[0]
            is True
        )

    assert files[0]["metadata"] == {"x-trace-id": _TRACE_ID}
    assert files[0]["headers"] == {"X-Trace-Id": _TRACE_ID}
    assert images[0]["headers"] == {"X-Trace-Id": _TRACE_ID}


def test_dialogs_media_send_outside_an_operation_carries_neither_channel(monkeypatch):
    """Absent, never empty: a send made outside an operation names none, on both channels."""
    _install_sdk_uuid(monkeypatch)
    files: list[dict] = []
    provider = _sdk_provider(SimpleNamespace(send_file_sync=lambda *_a, **kwargs: files.append(kwargs)))

    assert (
        provider.send_file(
            "peer-42",
            _stored_media(b"report", "report.pdf"),
            scope=scope_for("dialogs", "peer-42"),
            filename="report.pdf",
        )[0]
        is True
    )

    assert "headers" not in files[0]
    assert "metadata" not in files[0]


def test_dialogs_media_send_survives_an_sdk_that_rejects_the_upload_headers(monkeypatch, caplog):
    """The SDK is not in this tree, so `headers=` on a send is taken on trust.

    A chat that can no longer deliver a file is a worse outage than an upload the environment
    trace cannot correlate, so the rejection costs the header for the process — once, out loud,
    and without spending the attempt.
    """
    from ouroboros.observability import aef_tracing

    _install_sdk_uuid(monkeypatch)
    files: list[dict] = []

    def send_file(*_args, **kwargs):
        if "headers" in kwargs:
            raise TypeError("send_file_sync() got an unexpected keyword argument 'headers'")
        files.append(kwargs)

    provider = _sdk_provider(SimpleNamespace(send_file_sync=send_file))

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.outbound"), aef_tracing.trace_scope(_TRACE_ID):
        assert (
            provider.send_file(
                "peer-42", _stored_media(b"a", "a.pdf"), scope=scope_for("dialogs", "peer-42"), filename="a.pdf"
            )[0]
            is True
        )
        assert (
            provider.send_file(
                "peer-42", _stored_media(b"b", "b.pdf"), scope=scope_for("dialogs", "peer-42"), filename="b.pdf"
            )[0]
            is True
        )

    # Both files were delivered, and the gRPC channel kept the identifier the upload lost.
    assert [call["metadata"] for call in files] == [{"x-trace-id": _TRACE_ID}] * 2
    assert all("headers" not in call for call in files)
    # Warned once and then remembered, rather than re-attempted on every attachment.
    assert len([r for r in caplog.records if "rejected media trace headers" in r.getMessage()]) == 1
    assert provider._media_trace_headers is False


def test_dialogs_media_send_does_not_resend_on_a_type_error_from_inside_the_sdk(monkeypatch):
    """Only the interpreter's signature rejection is safe to retry without the keyword.

    It is raised before the SDK runs anything; a `TypeError` from inside a send may already have
    uploaded, so it is left to the send loop's own bounded retries rather than being re-sent here.
    """
    from ouroboros.observability import aef_tracing
    from transport.dialogs.outbound import _DIALOGS_MEDIA_SEND_ATTEMPTS

    _install_sdk_uuid(monkeypatch)
    calls: list[dict] = []

    def send_file(*_args, **kwargs):
        calls.append(kwargs)
        raise TypeError("cannot serialize headers of type dict")

    provider = _sdk_provider(SimpleNamespace(send_file_sync=send_file))

    with aef_tracing.trace_scope(_TRACE_ID):
        assert (
            provider.send_file(
                "peer-42", _stored_media(b"a", "a.pdf"), scope=scope_for("dialogs", "peer-42"), filename="a.pdf"
            )[0]
            is False
        )

    assert len(calls) == _DIALOGS_MEDIA_SEND_ATTEMPTS
    assert all("headers" in call for call in calls)
    assert provider._media_trace_headers is True


def test_dialogs_progress_update_carries_the_operation_as_grpc_metadata():
    """The progress edit is a send like any other; it went out bare while the sends did not."""
    from ouroboros.observability import aef_tracing

    anchor = SimpleNamespace(mid="progress-mid")
    updates: list[dict] = []
    provider = _sdk_provider(
        SimpleNamespace(
            send_message_sync=lambda *_a, **_k: anchor,
            get_message_by_id_sync=lambda *_a, **_k: anchor,
            update_message_sync=lambda *_a, **kwargs: updates.append(kwargs),
        ),
        DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED=True,
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        provider.send_text("peer-42", "шаг один", is_progress=True, task_id="task-1")
        provider.send_text("peer-42", "шаг два", is_progress=True, task_id="task-1")

    assert updates == [{"metadata": {"x-trace-id": _TRACE_ID}}]


class _TracingProvider(_DummyProvider):
    """Records the operation active where the SDK call would be made."""

    def __init__(self):
        super().__init__()
        self.observed: list[str] = []

    def send_text(self, chat_id, text: str, fmt: str = "", **kwargs):
        from ouroboros.observability import aef_tracing

        self.observed.append(aef_tracing.current_trace_id())
        return super().send_text(chat_id, text, fmt, **kwargs)


def test_dialogs_delivery_continues_the_operation_the_message_names(tmp_path):
    """The outbox is a boundary, so the identifier crosses it as data and is reopened here.

    Without this the reader thread — which inherits nothing — delivers every answer under no
    operation, and the metadata above has nothing to carry.
    """
    provider = _TracingProvider()
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="ответ", trace_id=_TRACE_ID))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.observed == [_TRACE_ID]
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_delivery_of_a_message_naming_no_operation_stays_outside_one(tmp_path):
    """Delivery does not originate: a minted identifier would name nothing to follow back to."""
    provider = _TracingProvider()
    transport = _dialogs_transport(tmp_path, provider)
    settled = _Delivery(_outbound(text="ответ"))

    transport.handle_outbound(settled.reader, settled.delivered)

    assert provider.observed == [""]
    assert (settled.acked, settled.nacked) == (1, 0)


def test_dialogs_typing_carries_the_operation_as_grpc_metadata(monkeypatch):
    """Presence is an outbound call of the same conversation and correlates with it."""
    from ouroboros.observability import aef_tracing

    presence = ModuleType("dialog_bot_sdk.entities.presence")
    presence.TypingType = SimpleNamespace(TYPINGTYPE_TEXT="text")
    monkeypatch.setitem(sys.modules, "dialog_bot_sdk.entities.presence", presence)
    typings: list[dict] = []
    provider = _sdk_provider(SimpleNamespace(), DIALOGS_TYPING_ENABLED=True)
    provider._bot.presence = SimpleNamespace(start_typing_sync=lambda *_a, **kwargs: typings.append(kwargs))

    with aef_tracing.trace_scope(_TRACE_ID):
        assert provider.send_typing("peer-42") is True

    assert typings == [{"metadata": {"x-trace-id": _TRACE_ID}}]


def test_dialogs_reads_carry_the_operation_as_grpc_metadata(monkeypatch):
    """What the agent asks the server for is a request it makes too.

    The history import, the sender's name and the reply lookup all fail in ways an operator
    reconstructs afterwards — the sender label silently degrading to `user-N`, a reply that
    started no task — so they are the calls a trace is most wanted for.
    """
    from ouroboros.observability import aef_tracing

    _install_sdk_uuid(monkeypatch)
    peer = SimpleNamespace(id="peer-42", type=1)
    histories: list[dict] = []
    lookups: list[dict] = []
    fetches: list[dict] = []
    provider = _sdk_provider(
        SimpleNamespace(
            load_message_history_sync=lambda *_a, **kwargs: histories.append(kwargs) or [],
            get_message_by_id_sync=lambda *_a, **kwargs: fetches.append(kwargs) or None,
        ),
        DIALOGS_HISTORY_RECOVERY_ENABLED=True,
        DIALOGS_PARTICIPANT_IDENTITY_ENABLED=True,
    )
    provider._bot.users = SimpleNamespace(
        get_user_by_id_sync=lambda *_a, **kwargs: lookups.append(kwargs) or SimpleNamespace(data=None)
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        provider._recover_peer_history(peer)
        provider._resolve_sender_label("7")
        provider._is_own_message(_SdkUUID(11, 22), "11:22")

    carried = {"metadata": {"x-trace-id": _TRACE_ID}}
    assert histories[0]["metadata"] == carried["metadata"]
    assert lookups == [carried]
    assert fetches == [carried]


def test_dialogs_progress_anchor_fetch_carries_the_operation_as_grpc_metadata():
    """The anchor lookup sits between two sends of the same operation and belongs to it."""
    from ouroboros.observability import aef_tracing

    anchor = SimpleNamespace(mid="progress-mid")
    fetches: list[dict] = []
    provider = _sdk_provider(
        SimpleNamespace(
            send_message_sync=lambda *_a, **_k: anchor,
            get_message_by_id_sync=lambda *_a, **kwargs: fetches.append(kwargs) or anchor,
            update_message_sync=lambda *_a, **_k: None,
        ),
        DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED=True,
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        provider.send_text("peer-42", "шаг один", is_progress=True, task_id="task-1")

    assert fetches == [{"metadata": {"x-trace-id": _TRACE_ID}}]


def test_dialogs_group_peer_lookup_carries_the_operation_as_grpc_metadata():
    """Peer resolution runs inside the operation that needed the peer, so it says so."""
    from ouroboros.observability import aef_tracing
    from transport.dialogs import DialogsProvider

    finds: list[dict] = []
    peer = SimpleNamespace(id="42", type=2)
    provider = DialogsProvider(_dialogs_settings(DIALOGS_GROUP_ID="42"))
    provider._bot = SimpleNamespace(
        groups=SimpleNamespace(
            find_group_by_id_sync=lambda *_a, **kwargs: finds.append(kwargs) or SimpleNamespace(peer=peer)
        ),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        assert provider._resolve_group_peer() is peer

    assert finds == [{"metadata": {"x-trace-id": _TRACE_ID}}]


def test_dialogs_file_download_carries_the_operation_as_an_http_header():
    """The file transfer is HTTP, not gRPC, so it takes the header and not the metadata key.

    Same identifier and the same rule about its absence; only the SDK's channel differs.
    """
    downloads: list[dict] = []

    def download_file(path, _document, **kwargs):
        downloads.append(kwargs)
        Path(path).write_text("report data", encoding="utf-8")

    provider = _download_provider(download_file)

    provider._on_file_passed(_dialogs_file_update(message_id="mid-trace-download"))
    provider._file_jobs.join()

    inbound = provider._incoming.get(timeout=2.0)
    try:
        # The ingress frame originated the operation; the download is the slow part of it.
        assert downloads == [{"headers": {"X-Trace-Id": inbound.trace_id}}]
    finally:
        Path(inbound.attachments[0].temp_path).unlink()


def test_dialogs_file_download_outside_an_operation_carries_no_header():
    """`_download_document` is reachable without an ingress frame; it must not invent one."""
    downloads: list[dict] = []

    def download_file(path, _document, **kwargs):
        downloads.append(kwargs)
        Path(path).write_text("report data", encoding="utf-8")

    provider = _download_provider(download_file)
    target = Path(tempfile.mkdtemp()) / "report.txt"

    provider._download_document(download_file, target, SimpleNamespace(), 1024, chat_id="peer-42")

    assert downloads == [{}]


def test_dialogs_update_stream_carries_an_identifier_it_minted_for_itself():
    """The subscription is an outbound gRPC call, so it identifies itself on the wire.

    Minted rather than inherited, the rule an MCP session follows: a stream that outlives every
    operation must not be labelled with whichever one happened to open it.
    """
    from ouroboros.observability import aef_tracing

    opens: list[dict] = []
    provider = _sdk_provider(SimpleNamespace())
    provider._bot.updates = SimpleNamespace(on_updates=lambda **kwargs: opens.append(kwargs))

    with aef_tracing.trace_scope(_TRACE_ID):
        provider._open_updates_stream(provider._bot)

    carried = opens[0]["metadata"]["x-trace-id"]
    assert aef_tracing.normalize_trace_id(carried) == carried
    assert carried != _TRACE_ID
    assert opens[0]["do_read_message"] is True
    # The scope ends with the call, so an arriving update still originates its own.
    assert aef_tracing.current_trace_id() == ""


def test_dialogs_update_stream_survives_an_sdk_that_rejects_outbound_metadata(caplog):
    """Losing the header must cost the header, not the whole inbound path.

    Any exception out of `on_updates` reads to the loop as an outage it retries every five
    seconds forever, so an SDK without the kwarg would silently stop the bot receiving anything.
    """
    opens: list[dict] = []

    def on_updates(**kwargs):
        if "metadata" in kwargs:
            raise TypeError("on_updates() got an unexpected keyword argument 'metadata'")
        opens.append(kwargs)

    provider = _sdk_provider(SimpleNamespace())
    provider._bot.updates = SimpleNamespace(on_updates=on_updates)

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.inbound"):
        provider._open_updates_stream(provider._bot)
        provider._open_updates_stream(provider._bot)

    assert opens == [{"do_read_message": True, "do_register_commands": True}] * 2
    # Warned once and then remembered, rather than retried on every reconnect.
    assert len([r for r in caplog.records if "rejected trace metadata" in r.getMessage()]) == 1
    assert provider._updates_trace_metadata is False


def test_dialogs_grpc_metadata_says_which_agent_is_calling(monkeypatch):
    """The environment trace reads `x-agent-id` off the request like `x-trace-id`; gRPC takes
    both as metadata, lowercased, because it rejects a key that is not."""
    from ouroboros.observability import aef_tracing
    from ouroboros.storage.settings import store

    monkeypatch.setattr(
        store,
        "load_settings",
        lambda: SimpleNamespace(AGENT_SYSTEM_CONFIGURATION_AGENT_ID="CI0000123", AGENT_ID=""),
    )
    sends: list[dict] = []
    downloads: list[dict] = []
    provider = _sdk_provider(SimpleNamespace(send_message_sync=lambda *_a, **kwargs: sends.append(kwargs)))

    def download_file(path, _document, **kwargs):
        downloads.append(kwargs)
        Path(path).write_text("d", encoding="utf-8")

    with aef_tracing.trace_scope(_TRACE_ID):
        provider.send_text("peer-42", "ответ")
        provider._download_document(
            download_file, Path(tempfile.mkdtemp()) / "f.txt", SimpleNamespace(), 1024, chat_id="peer-42"
        )

    assert sends[0]["metadata"] == {"x-trace-id": _TRACE_ID, "x-agent-id": "CI0000123"}
    # The file transfer is HTTP, so it takes the canonical spelling on the other channel.
    assert downloads[0]["headers"] == {"X-Trace-Id": _TRACE_ID, "X-Agent-Id": "CI0000123"}


def test_dialogs_bot_config_carries_metadata_for_the_calls_nobody_stamps(monkeypatch):
    """Token auth and device registration happen before this transport has anything to say.

    They are the SDK's own RPCs, so the only place to identify them is the bot: the SDK puts
    bot-level metadata on every call that passes none of its own.
    """
    from ouroboros.observability import aef_tracing
    from ouroboros.storage.settings import store
    from transport.dialogs import DialogsProvider

    monkeypatch.setattr(
        store,
        "load_settings",
        lambda: SimpleNamespace(AGENT_SYSTEM_CONFIGURATION_AGENT_ID="CI0000123", AGENT_ID=""),
    )
    provider = DialogsProvider(
        _dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443", DIALOGS_BOT_TOKEN="token")
    )

    with aef_tracing.trace_scope(_TRACE_ID):
        metadata = provider._build_bot_config()["metadata"]

    assert metadata["x-agent-id"] == "CI0000123"
    # Minted for the connection, never inherited: a bot outlives every operation, so the task
    # that happened to build it must not name the whole session's service traffic.
    assert aef_tracing.normalize_trace_id(metadata["x-trace-id"]) == metadata["x-trace-id"]
    assert metadata["x-trace-id"] != _TRACE_ID


def test_dialogs_bot_still_builds_when_the_sdk_rejects_bot_level_metadata(caplog):
    """Losing the header on service calls must cost the header, not the transport: a bot that
    never builds leaves the agent with no ingress and no egress at all."""
    from transport.dialogs import DialogsProvider

    seen: list[dict] = []

    class _DialogBot:
        @staticmethod
        def create_bot(bot_config):
            if "metadata" in bot_config:
                raise TypeError("__init__() got an unexpected keyword argument 'metadata'")
            seen.append(bot_config)
            return SimpleNamespace(messaging=SimpleNamespace())

    provider = DialogsProvider(
        _dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443", DIALOGS_BOT_TOKEN="token")
    )

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.provider"):
        bot = provider._create_sdk_bot(_DialogBot, provider._build_bot_config())

    assert bot is not None
    assert seen and "metadata" not in seen[0]
    assert "rejected bot-level trace metadata" in caplog.text


def test_dialogs_bot_build_failure_that_is_not_about_metadata_is_not_retried(caplog):
    """The fallback is for one rejected keyword, not a blanket retry.

    A second attempt after an unrelated failure would build a second channel for a bot that is
    going to raise anyway, and would report the failure as something it is not.
    """
    from transport.dialogs import DialogsProvider

    attempts: list[dict] = []

    class _DialogBot:
        @staticmethod
        def create_bot(bot_config):
            attempts.append(bot_config)
            raise TypeError("something else entirely")

    provider = DialogsProvider(
        _dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443", DIALOGS_BOT_TOKEN="token")
    )

    with caplog.at_level(logging.WARNING, logger="transport.dialogs.provider"), pytest.raises(TypeError):
        provider._create_sdk_bot(_DialogBot, provider._build_bot_config())

    assert len(attempts) == 1
    assert "rejected bot-level trace metadata" not in caplog.text
