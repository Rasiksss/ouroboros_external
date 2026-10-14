import base64
from contextlib import ExitStack

from ouroboros.storage.file_transfers import TransferLimits, get_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.files import stored_file
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE
from transport.browser.identity import CHAT_HEADER_NAME, OWNER_HEADER_NAME, TrustedHeaderIdentity, browser_identity
from transport.browser.ws import get_browser_ws_hub
from transport.infra.inmemory import InProcessOutbox
from transport.interfaces import AttachmentRef, OutboundKind, OutboundMessage, SubscriptionMode


def test_deferred_photo_allows_another_connected_owner_to_receive_text(tmp_path, monkeypatch):
    monkeypatch.setattr("transport.browser.ws._ADMISSION_RETRY_BACKOFF_SEC", 0.2)
    previous = browser_identity.resolver
    content = b"\x89PNG\r\n\x1a\nphoto"
    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True) as client:
        browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
        try:
            file = stored_file(data=content, filename="photo.png", mime="image/png")
            other_replica = TransferLimits(
                runtime_persistence.current.engine, 8, 2, process_id="other-replica", lease_seconds=300
            )
            outbox = InProcessOutbox()
            reader = outbox.reader(name="media-admission", mode=SubscriptionMode.BROADCAST)
            with ExitStack() as stack:
                stack.callback(reader.close)
                sockets = [
                    stack.enter_context(
                        client.websocket_connect(
                            "/ws",
                            headers={
                                OWNER_HEADER_NAME: owner,
                                CHAT_HEADER_NAME: TEST_SCOPE.chat_id,
                            },
                        )
                    )
                    for owner in (TEST_SCOPE.owner_id, "other-owner")
                ]
                hub = get_browser_ws_hub()
                outbox.publish(
                    OutboundMessage(
                        scope=TEST_SCOPE,
                        source="browser",
                        kind=OutboundKind.PHOTO,
                        attachment=AttachmentRef(attachment_id=file["id"]),
                    )
                )
                outbox.publish(OutboundMessage(scope=TEST_SCOPE, source="browser", text="after photo"))
                outbox.publish(
                    OutboundMessage(
                        scope=TEST_SCOPE.model_copy(update={"owner_id": "other-owner"}),
                        source="browser",
                        text="other text",
                    )
                )
                with get_transfer_limits().slot(TEST_SCOPE), other_replica.slot(TEST_SCOPE, outgoing=True):
                    photo = reader.poll(0)
                    assert photo is not None
                    reader.nack(photo)
                    photo = reader.poll(0)
                    assert photo is not None and photo.attempts == 1
                    hub.handle_outbound(reader, photo)
                    other = reader.poll(0)
                    assert other is not None and other.message.text == "other text"
                    hub.handle_outbound(reader, other)
                    assert sockets[1].receive_json()["content"] == "other text"
                    assert reader.poll(0) is None
                    for _ in range(3):
                        deferred = reader.poll(0.5)
                        assert deferred is not None and deferred.attempts == 1
                        hub.handle_outbound(reader, deferred)
                resumed = reader.poll(0.5)
                assert resumed is not None and resumed.message.kind is OutboundKind.PHOTO
                assert resumed.attempts == 1
                hub.handle_outbound(reader, resumed)
                frame = sockets[0].receive_json()
                assert frame["type"] == "photo"
                assert base64.b64decode(frame["image_base64"]) == content
                following = reader.poll(0)
                assert following is not None and following.message.text == "after photo"
                hub.handle_outbound(reader, following)
                assert sockets[0].receive_json()["content"] == "after photo"
        finally:
            browser_identity.initialize(previous)
