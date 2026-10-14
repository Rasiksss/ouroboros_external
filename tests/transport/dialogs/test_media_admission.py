from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ouroboros.contracts.attachments import StoredFile
from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
from ouroboros.storage.blob.factory import get_storage_provider, install_storage_provider_builder_for_tests
from ouroboros.storage.file_catalog import FileTransferPending
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.blob_double import InMemoryStorageProvider
from tests.helpers.dialogs_cursor import sql_dialogs_cursor_repository
from tests.helpers.fake_gateway import FakeGateway
from tests.helpers.files import stored_file
from transport.dialogs.provider import DialogsProvider
from transport.dialogs.state import dialogs_scope
from transport.dialogs.transport import DialogsTransport
from transport.interfaces import AttachmentRef, OutboundKind, OutboundMessage, SubscriptionMode


def test_media_waits_for_capacity_without_spending_delivery_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", "2")
    monkeypatch.setattr("transport.dialogs.transport._DELIVERY_RETRY_BACKOFF_SEC", 0.2)
    sent, texts = [], []
    gateway = FakeGateway()
    settings = AppSettings(DIALOGS_GRPC_ENDPOINT="grpc://test", DIALOGS_BOT_TOKEN="token")
    provider = DialogsProvider(settings)
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(
            send_file_sync=lambda *args, **kwargs: sent.append(args) or "sent",
            send_message_sync=lambda peer, text, **kwargs: texts.append((peer.id, text)) or "sent",
        )
    )
    provider._out_peer_by_chat_id["recipient"] = SimpleNamespace(id="recipient")
    provider._out_peer_by_chat_id["other"] = SimpleNamespace(id="other")
    transport = DialogsTransport(
        settings=settings,
        gateway=gateway,
        storage=get_storage_provider(),
        drive_root=tmp_path,
        session_id="test",
        provider_factory=lambda _: provider,
        cursor_repository=sql_dialogs_cursor_repository(),
    )
    transport.provider = provider
    scope = dialogs_scope("recipient")
    file = stored_file(scope=scope)
    reader = gateway.outbox.reader(name="dialogs", mode=SubscriptionMode.SHARED)
    gateway.outbox.publish(
        OutboundMessage(
            scope=scope,
            source="dialogs",
            kind=OutboundKind.FILE,
            attachment=AttachmentRef(attachment_id=file["id"]),
        )
    )
    gateway.outbox.publish(OutboundMessage(scope=scope, source="dialogs", text="following"))
    gateway.outbox.publish(
        OutboundMessage(
            scope=scope.model_copy(update={"owner_id": "other-owner", "chat_id": "other"}),
            source="dialogs",
            text="other text",
        )
    )
    limits = get_transfer_limits()
    try:
        delivered = reader.poll(0)
        reader.nack(delivered)
        delivered = reader.poll(0)
        assert delivered.attempts == 1
        with limits.slot(scope), limits.slot(scope):
            transport.handle_outbound(reader, delivered)
            other = reader.poll(0)
            assert other is not None and other.message.text == "other text"
            transport.handle_outbound(reader, other)
            assert texts == [("other", "other text")]
            assert not sent
            assert reader.poll(0) is None
        resumed = reader.poll(0.5)
        assert resumed is not None and resumed.attempts == 1
        transport.handle_outbound(reader, resumed)
        assert len(sent) == 1
        following = reader.poll(0)
        assert following is not None and following.message.text == "following"
        transport.handle_outbound(reader, following)
        assert texts[-1] == ("recipient", "following")
    finally:
        reader.close()


@pytest.mark.parametrize("preparing", [True, False])
def test_media_timeout_distinguishes_preparation_and_keeps_worker_file_alive(monkeypatch, preparing):
    entered, release = Event(), Event()
    paths, sent = [], []

    class Storage(InMemoryStorageProvider):
        def copy_to_path(self, owner, scope, key, path, **kwargs):
            super().copy_to_path(owner, scope, key, path, **kwargs)
            paths.append(path)
            if preparing:
                entered.set()
                assert release.wait(5)

    def sdk(peer, path, **kwargs):
        sent.append(path)
        if not preparing:
            paths.append(Path(path))
            entered.set()
            assert release.wait(5)
        assert Path(path).read_bytes() == b"hello"
        return "sent"

    install_storage_provider_builder_for_tests(Storage)
    scope = dialogs_scope("recipient")
    file = stored_file(scope=scope)
    install_storage_provider_builder_for_tests(InMemoryStorageProvider)
    provider = DialogsProvider(AppSettings(DIALOGS_GRPC_ENDPOINT="grpc://test", DIALOGS_BOT_TOKEN="token"))
    provider._bot = SimpleNamespace(messaging=SimpleNamespace(send_file_sync=sdk))
    provider._out_peer_by_chat_id["recipient"] = SimpleNamespace(id="recipient")
    monkeypatch.setattr("transport.dialogs.outbound._DIALOGS_OUTBOUND_TIMEOUT_SEC", 0.1)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            provider.send_file, "recipient", StoredFile(file["id"]), filename=None, mime=None, scope=scope
        )
        try:
            assert entered.wait(5)
            if preparing:
                with pytest.raises(FileTransferPending):
                    future.result(timeout=5)
            else:
                assert future.result(timeout=5) == (False, DELIVERY_UNKNOWN)
            assert paths[0].exists()
        finally:
            with provider._incoming_lock:
                workers = list(provider._outbound_workers)
            release.set()
            for worker in workers:
                worker.join(5)
        assert not paths[0].exists()
        assert len(sent) == (0 if preparing else 1)
