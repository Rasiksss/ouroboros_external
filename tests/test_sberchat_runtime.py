from __future__ import annotations

import logging
import sys
import threading
import traceback
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import SecretStr

from ouroboros.storage.settings.schema import AppSettings
from supervisor.messaging.base import InboundAttachment, InboundMessage


def test_dialogs_package_exports_provider() -> None:
    from supervisor.messaging.dialogs import DialogsProvider
    from supervisor.messaging.dialogs.provider import _dialogs_bot_token

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


def _runtime_transport(provider, **settings):
    from supervisor.server_runtime import build_server_runtime_transport

    return build_server_runtime_transport(
        settings=_dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
            **settings,
        ),
        bridge=_DummyBridge(),
        logger=SimpleNamespace(warning=lambda *_a, **_k: None, info=lambda *_a, **_k: None),
        dialogs_provider_factory=lambda _cfg: provider,
    )


def _sdk_provider(messaging, **settings):
    from supervisor.messaging.dialogs import DialogsProvider

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


def _download_provider(download, **settings):
    from supervisor.messaging.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(**settings))
    provider._bot = SimpleNamespace(
        internal=SimpleNamespace(downloading=SimpleNamespace(download_file_sync=download)),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )
    return provider


def _runtime_context(tmp_path, *, cursor: int | None = None, submit=None, save_state=None):
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from supervisor import state
    from supervisor.state import default_state_dict

    repo = state.init(tmp_path)
    initial = OwnerRuntimeState.model_validate(default_state_dict())
    if cursor is not None:
        initial.provider_state = {"dialogs": {"seq": cursor}}
    repo.save(initial)
    captured = []
    submit = submit or (
        lambda envelope: captured.append(envelope) or SimpleNamespace(intent="chat", task_id="task-inbound")
    )
    return (
        repo,
        SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            load_state=repo.load,
            save_state=save_state or repo.save,
            send_with_budget=lambda *_a, **_k: None,
            bridge=SimpleNamespace(broadcast=lambda *_a, **_k: None),
            orchestrator=SimpleNamespace(submit=submit),
        ),
        captured,
    )


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
    from supervisor.messaging.dialogs.outbound import (
        MAX_DIALOGS_MESSAGE_CHARS,
        _split_dialogs_message,
    )

    chunks = _split_dialogs_message("x" * 7601)

    assert MAX_DIALOGS_MESSAGE_CHARS == 3800
    assert len(chunks) == 3
    assert max(len(chunk) for chunk in chunks) <= 3800


def test_dialogs_runtime_disabled_without_endpoint_or_token(tmp_path):
    from supervisor.server_runtime import build_server_runtime_transport

    transport = build_server_runtime_transport(
        settings=_dialogs_settings(),
        bridge=_DummyBridge(),
        logger=SimpleNamespace(warning=lambda *_a, **_k: None, info=lambda *_a, **_k: None),
        dialogs_provider_factory=lambda _cfg: (_ for _ in ()).throw(AssertionError("provider must stay disabled")),
    )

    assert transport.provider_active is False
    assert transport.provider is None


def test_dialogs_transport_starts_provider_and_sends_text(tmp_path):
    provider = _DummyProvider()
    transport = _runtime_transport(provider)

    transport.send_with_budget("peer-42", "hello", fmt="markdown")

    assert transport.provider_active is True
    assert provider.connect_calls == 1
    assert provider.authenticate_calls == 1
    assert provider.text_calls == [("peer-42", "hello", "markdown", {})]


def test_dialogs_transport_lifecycle_logs_only_state_changes(caplog):
    from supervisor.server_runtime import ensure_dialogs_runtime_transport_provider

    caplog.set_level(logging.INFO)
    transport = _runtime_transport(_DummyProvider())
    ensure_dialogs_runtime_transport_provider(transport)
    ensure_dialogs_runtime_transport_provider(transport)
    transport.close()
    transport.close()

    assert caplog.text.count("event=dialogs_transport_started") == 1
    assert caplog.text.count("event=dialogs_transport_stopped") == 1
    assert "Dialogs transport reusing existing provider" not in caplog.text


def test_dialogs_updates_reconnect_logs_only_transitions(caplog):
    from supervisor.messaging.dialogs import DialogsProvider

    stop_state = SimpleNamespace(stopped=False)
    stop_state.is_set = lambda: stop_state.stopped
    stop_state.wait = lambda _timeout: stop_state.stopped
    calls = 0

    def on_updates(**_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError()
        if calls == 4:
            stop_state.stopped = True

    provider = DialogsProvider(_dialogs_settings())
    provider._bot = SimpleNamespace(updates=SimpleNamespace(on_updates=on_updates))
    provider._updates_stop_event = stop_state
    caplog.set_level(logging.INFO)

    provider._updates_loop()

    assert caplog.text.count("event=dialogs_transport_unavailable") == 1
    assert caplog.text.count("event=dialogs_transport_recovery_started") == 1
    assert caplog.text.count("event=dialogs_transport_restored") == 1
    assert "Dialogs provider updates subscription active" not in caplog.text


def test_dialogs_transport_accepts_task_source_metadata(tmp_path):
    provider = _DummyProvider()
    transport = _runtime_transport(provider)

    transport.send_with_budget("peer-42", "hello", fmt="markdown", source="dialogs")

    assert provider.text_calls == [("peer-42", "hello", "markdown", {})]


def test_dialogs_provider_preserves_zero_chat_id():
    provider = _sdk_provider(SimpleNamespace())
    provider.settings = _dialogs_settings(DIALOGS_GROUP_ID="0")
    peer = SimpleNamespace(id=0)
    provider._remember_out_peer("0", peer)

    assert provider._group_id() == 0
    assert provider._resolve_out_peer(0) is peer


def test_dialogs_transport_sends_images_and_files(tmp_path):
    provider = _DummyProvider()
    transport = _runtime_transport(provider)

    assert transport.send_image("peer-42", b"png", filename="result.png", caption="image") == (
        True,
        "image-mid",
    )
    assert transport.send_file(
        "peer-42",
        b"pdf",
        filename="report.pdf",
        mime="application/pdf",
        caption="file",
    ) == (True, "file-mid")
    assert provider.image_calls[0][2]["filename"] == "result.png"
    assert provider.file_calls[0][2]["mime"] == "application/pdf"


def test_dialogs_transport_passes_opt_in_reply_metadata_to_files(tmp_path):
    provider = _DummyProvider()
    transport = _runtime_transport(
        provider,
        DIALOGS_REPLY_ENABLED=True,
        DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED=True,
    )

    transport.send_file(
        "peer-42",
        b"pdf",
        filename="report.pdf",
        transport={"source_message_id": "mid-1", "target_uid": "user-1"},
    )

    assert provider.file_calls[0][2]["reply_to"] == ["mid-1"]
    assert provider.file_calls[0][2]["target_uid"] == "user-1"


def test_dialogs_provider_forwarding_is_allowed_by_default():
    calls = []
    provider = _sdk_provider(SimpleNamespace(send_file_sync=lambda *args, **kwargs: calls.append((args, kwargs))))

    provider.send_file("peer-42", b"report", filename="report.pdf", mime="application/pdf")

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

    result = provider.send_file("peer-42", b"report", filename="report.pdf")

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

    result = provider.send_file("peer-42", b"report", filename="report.pdf")

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

    with caplog.at_level(logging.INFO, logger="supervisor.messaging.dialogs.outbound"):
        result = provider.send_file("peer-42", b"private report body", filename="report.txt", task_id="task-1")

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

    with caplog.at_level(logging.WARNING, logger="supervisor.messaging.dialogs.outbound"):
        result = provider.send_image("peer-42", b"image-body", filename="image.png")

    assert result[0] is False
    operational = "\n".join(record.getMessage() for record in caplog.records)
    assert operational.count("event=dialogs_image_send_failed") == 2
    assert "stage=dialogs_send_image_sync attempt=1 exception_type=ConnectionResetError" in operational
    assert "message=sdk_call_failed duration=" in operational
    assert "super-secret" not in operational
    assert "image-body" not in operational


@pytest.mark.parametrize("kind", ["text", "file"])
def test_dialogs_provider_times_out_sync_delivery_and_defers_media_cleanup(
    monkeypatch,
    kind,
):
    from supervisor.messaging.dialogs import outbound as provider_module

    started = threading.Event()
    finish = threading.Event()
    paths = []

    def blocked(*args, **_kwargs):
        if kind == "file":
            paths.append(Path(args[1]))
        started.set()
        finish.wait(1.0)
        return SimpleNamespace(mid="mid-1")

    method = "send_message_sync" if kind == "text" else "send_file_sync"
    provider = _sdk_provider(SimpleNamespace(**{method: blocked}))
    monkeypatch.setattr(
        provider_module,
        "_DIALOGS_OUTBOUND_TIMEOUT_SEC",
        0.01,
        raising=False,
    )

    result = (
        provider.send_text("peer-42", "hello")
        if kind == "text"
        else provider.send_file("peer-42", b"report", filename="report.txt")
    )

    assert started.is_set()
    assert result[0] is False
    if kind == "file":
        from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN

        assert result[1] == DELIVERY_UNKNOWN
    else:
        assert "timed out" in result[1]
    if paths:
        assert paths[0].exists()
    finish.set()
    if paths:
        for _ in range(100):
            if not paths[0].exists():
                break
            threading.Event().wait(0.01)
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
    file_result = provider.send_file("peer-42", b"report", filename="report.pdf", reply_to=["11:22"], target_uid="42")

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
    assert provider.send_file("peer-42", b"report", filename="report.pdf", reply_to=["legacy-id"])[0] is True
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

    with caplog.at_level(logging.WARNING, logger="supervisor.messaging.dialogs.outbound"):
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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor import state
    from supervisor.server_runtime import handle_runtime_inbound
    from supervisor.state import default_state_dict

    init_runtime_store(tmp_path)
    repo = state.init(tmp_path)
    repo.save(OwnerRuntimeState.model_validate(default_state_dict()))
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        load_state=repo.load,
        save_state=repo.save,
        send_with_budget=lambda *_a, **_k: None,
        bridge=SimpleNamespace(broadcast=lambda *_a, **_k: None),
        orchestrator=SimpleNamespace(
            submit=lambda _envelope: (_ for _ in ()).throw(AssertionError("history must not start a task"))
        ),
    )

    assert handle_runtime_inbound(
        source="dialogs",
        cursor=17,
        message=InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="@ouroboros_bot собери отчёт",
            source_message_id="old-mid",
            history_only=True,
        ),
        ctx=ctx,
    )


def test_typing_stop_event_uses_presence_cancel_action():
    from supervisor import events

    calls = []
    ctx = SimpleNamespace(
        send_typing=lambda *args, **kwargs: calls.append((args, kwargs)),
        bridge=SimpleNamespace(send_chat_action=lambda *_a, **_k: None),
    )

    events._handle_typing_stop({"chat_id": 0, "source": "dialogs"}, ctx)

    assert calls == [((0, "cancel"), {"source": "dialogs"})]


def test_dialogs_fanout_routes_by_task_source(tmp_path):
    from supervisor.server_runtime import attach_dialogs_transport_fanout, build_server_runtime_transport

    provider = _DummyProvider()
    transport = build_server_runtime_transport(
        settings=_dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        ),
        bridge=_DummyBridge(),
        logger=SimpleNamespace(warning=lambda *_a, **_k: None, info=lambda *_a, **_k: None),
        dialogs_provider_factory=lambda _cfg: provider,
    )
    local_calls = []
    event_ctx = SimpleNamespace(
        send_with_budget=lambda *args, **kwargs: local_calls.append((args, kwargs)),
        send_typing=None,
        send_photo=None,
    )
    attach_dialogs_transport_fanout(
        event_ctx,
        transport,
        lambda: SimpleNamespace(owner_chat_source="dialogs", owner_chat_id="peer-42"),
    )

    event_ctx.send_with_budget("peer-42", "browser reply", source="browser")
    event_ctx.send_with_budget("peer-42", "dialogs reply", source="dialogs")
    event_ctx.send_with_budget("peer-42", "sberchat reply", source="sberchat")

    assert [call[1] for call in local_calls] == [
        {"source": "browser"},
        {"source": "dialogs"},
        {"source": "sberchat"},
    ]
    assert provider.text_calls == [
        ("peer-42", "dialogs reply", "", {}),
        ("peer-42", "sberchat reply", "", {}),
    ]


def test_dialogs_text_fanout_does_not_record_failed_external_delivery():
    from supervisor.server_runtime import attach_dialogs_transport_fanout

    local_calls = []
    event_ctx = SimpleNamespace(
        send_with_budget=lambda *args, **kwargs: local_calls.append((args, kwargs)),
        send_typing=None,
        send_photo=None,
    )
    runtime_transport = SimpleNamespace(
        provider=None,
        send_with_budget=lambda *_args, **_kwargs: (False, "dialogs unavailable"),
    )
    attach_dialogs_transport_fanout(
        event_ctx,
        runtime_transport,
        lambda: SimpleNamespace(owner_chat_source="dialogs", owner_chat_id="group-42"),
    )

    with pytest.raises(RuntimeError, match="dialogs unavailable"):
        event_ctx.send_with_budget("group-42", "reply", source="dialogs")

    assert local_calls == []


@pytest.mark.parametrize(
    ("kind", "payload", "filename"),
    [("file", b"report", "report.txt"), ("image", b"png", "image.png")],
)
@pytest.mark.parametrize("failed_side", ["external", "local"])
def test_dialogs_media_fanout_is_external_first(kind, payload, filename, failed_side):
    from supervisor.server_runtime import attach_dialogs_transport_fanout

    calls = []

    def external(*_args, **_kwargs):
        calls.append("external")
        return (False, "dialogs unavailable") if failed_side == "external" else (True, "mid-1")

    def local(*_args, **_kwargs):
        calls.append("local")
        return (False, "failed to persist attachment") if failed_side == "local" else (True, "ok")

    event_ctx = SimpleNamespace(
        send_with_budget=lambda *_args, **_kwargs: (True, "ok"),
        send_typing=None,
        send_photo=None,
        **{f"send_{kind}": local},
    )
    runtime_transport = SimpleNamespace(
        provider=None,
        **{f"send_{kind}": external},
    )
    attach_dialogs_transport_fanout(
        event_ctx,
        runtime_transport,
        lambda: SimpleNamespace(owner_chat_source="dialogs", owner_chat_id="group-42"),
    )

    result = getattr(event_ctx, f"send_{kind}")(
        "group-42",
        payload,
        filename=filename,
        source="dialogs",
    )

    expected = (False, "dialogs unavailable") if failed_side == "external" else (True, "mid-1")
    assert result == expected
    assert calls == (["external"] if failed_side == "external" else ["external", "local"])


def test_handle_runtime_inbound_persists_cursor_and_preserves_string_chat_id(tmp_path):
    from supervisor import state
    from supervisor.server_runtime import handle_runtime_inbound

    repo = state.init(tmp_path)
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from supervisor.state import default_state_dict

    repo.save(OwnerRuntimeState.model_validate(default_state_dict()))
    observed = {}
    called = threading.Event()

    def handle_chat_direct(chat_id, text, image_data=None):
        observed.update(chat_id=chat_id, text=text, image_data=image_data)
        called.set()

    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        load_state=repo.load,
        save_state=repo.save,
        send_with_budget=lambda *_a, **_k: None,
        get_chat_agent=lambda: SimpleNamespace(_busy=False),
        handle_chat_direct=handle_chat_direct,
        bridge=SimpleNamespace(broadcast=lambda *_a, **_k: None),
        runtime_transport=SimpleNamespace(
            settings=SimpleNamespace(DIALOGS_GROUP_ID="peer-42"),
            provider=None,
        ),
    )

    handled = handle_runtime_inbound(
        source="dialogs",
        message=InboundMessage(chat_id="peer-42", user_id="peer-42", text="hello"),
        cursor=15,
        ctx=ctx,
    )
    st = repo.load()

    assert handled is True
    assert called.wait(1.0)
    assert st.owner_chat_id == "peer-42"
    assert st.owner_chat_source == "dialogs"
    assert st.provider_state["dialogs"]["seq"] == 15
    assert observed["chat_id"] == "peer-42"


def test_handle_runtime_inbound_persists_file_and_passes_only_attachment_metadata(tmp_path, caplog):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.server_runtime import handle_runtime_inbound

    incoming_path = tmp_path / "incoming.pdf"
    incoming_path.write_bytes(b"pdf-data")
    _repo, ctx, captured = _runtime_context(tmp_path)
    caplog.set_level(logging.INFO)

    handled = handle_runtime_inbound(
        source="dialogs",
        cursor=16,
        message=_file_message(incoming_path, message_id="mid-16"),
        ctx=ctx,
    )

    assert handled is True
    assert not incoming_path.exists()
    assert len(captured) == 1
    envelope = captured[0]
    assert len(envelope.attachment_ids) == 1
    assert envelope.image_base64 == ""
    assert "storage_key" in envelope.attachments[0]
    provider = create_storage_provider(data_dir=tmp_path)
    assert provider.get_bytes(SCOPE_UPLOADS, envelope.attachments[0]["storage_key"]) == b"pdf-data"
    stored_message = list_owner_messages(tmp_path)[-1]
    assert stored_message.metadata["attachments"][0]["id"] == envelope.attachment_ids[0]
    assert stored_message.metadata["attachments"][0]["message_id"] == "mid-16"
    assert (
        "event=inbound_attachment_bound task_id=task-inbound filename=report.pdf size=8 source=dialogs" in caplog.text
    )


def test_handle_runtime_inbound_keeps_file_and_cursor_for_retry_when_persistence_fails(
    monkeypatch,
    tmp_path,
):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from supervisor import server_runtime

    repo, ctx, _captured = _runtime_context(tmp_path, cursor=16)
    incoming_path = tmp_path / "incoming.pdf"
    incoming_path.write_bytes(b"pdf-data")
    monkeypatch.setattr(
        server_runtime,
        "append_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    with pytest.raises(OSError, match="database unavailable"):
        server_runtime.handle_runtime_inbound(
            source="dialogs",
            cursor=17,
            message=_file_message(incoming_path, message_id="mid-17"),
            ctx=ctx,
        )

    provider = create_storage_provider(data_dir=tmp_path)
    assert provider.list_keys(SCOPE_UPLOADS, "chat/") == []
    assert provider.list_keys(SCOPE_UPLOADS, "_meta/chat/") == []
    assert incoming_path.exists()
    assert repo.load().provider_state["dialogs"]["seq"] == 16


def test_handle_runtime_inbound_attachment_replay_is_idempotent(tmp_path):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.server_runtime import handle_runtime_inbound

    repo, ctx, submitted = _runtime_context(tmp_path)

    for cursor in (21, 22):
        incoming_path = tmp_path / f"incoming-{cursor}.pdf"
        incoming_path.write_bytes(b"pdf-data")
        assert handle_runtime_inbound(
            source="dialogs",
            cursor=cursor,
            message=_file_message(incoming_path, message_id="same-mid"),
            ctx=ctx,
        )
        assert not incoming_path.exists()

    provider = create_storage_provider(data_dir=tmp_path)
    assert len(submitted) == 1
    assert len(list_owner_messages(tmp_path)) == 1
    assert len(provider.list_keys(SCOPE_UPLOADS, "chat/")) == 1
    assert len(provider.list_keys(SCOPE_UPLOADS, "_meta/chat/")) == 1
    assert repo.load().provider_state["dialogs"]["seq"] == 22


def test_handle_runtime_inbound_can_retry_after_orchestrator_failure(tmp_path):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.server_runtime import handle_runtime_inbound

    attempts = 0

    def submit(_envelope):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("task queue unavailable")
        return SimpleNamespace(intent="chat")

    repo, ctx, _captured = _runtime_context(tmp_path, cursor=30, submit=submit)
    incoming_path = tmp_path / "retry.pdf"
    incoming_path.write_bytes(b"pdf-data")
    message = _file_message(incoming_path, message_id="retry-mid")

    with pytest.raises(OSError, match="task queue unavailable"):
        handle_runtime_inbound(source="dialogs", cursor=31, message=message, ctx=ctx)

    provider = create_storage_provider(data_dir=tmp_path)
    assert incoming_path.exists()
    pending_messages = list_owner_messages(tmp_path)
    assert len(pending_messages) == 1
    assert pending_messages[0].metadata["inbound_processed"] is False
    assert len(provider.list_keys(SCOPE_UPLOADS, "chat/")) == 1
    assert repo.load().provider_state["dialogs"]["seq"] == 30

    assert handle_runtime_inbound(source="dialogs", cursor=31, message=message, ctx=ctx)
    assert attempts == 2
    assert not incoming_path.exists()
    assert len(list_owner_messages(tmp_path)) == 1
    assert len(provider.list_keys(SCOPE_UPLOADS, "chat/")) == 1
    assert repo.load().provider_state["dialogs"]["seq"] == 31


def test_handle_runtime_inbound_retries_after_cursor_save_without_duplicate_task_or_blob(
    tmp_path,
):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_store import list_owner_messages
    from ouroboros.storage.runtime_models import OwnerRuntimeState
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor import state
    from supervisor.orchestrator.orchestrator import TaskOrchestrator
    from supervisor.queue import TaskQueue
    from supervisor.server_runtime import handle_runtime_inbound
    from supervisor.state import RuntimeStateRepository, default_state_dict

    store = init_runtime_store(tmp_path)
    repo = state.init(tmp_path)
    initial_state = OwnerRuntimeState.model_validate(default_state_dict())
    initial_state.provider_state = {"dialogs": {"seq": 40}}
    repo.save(initial_state)
    task_state_repo = RuntimeStateRepository(tmp_path, store=store)
    orchestrator = TaskOrchestrator(
        store=store,
        task_queue=TaskQueue(store, tmp_path, state_repo=task_state_repo),
        state_repo=task_state_repo,
        agent_getter=lambda: None,
    )
    save_calls = 0

    def save_state(value):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("cursor store unavailable")
        repo.save(value)

    incoming_path = tmp_path / "cursor-retry.pdf"
    incoming_path.write_bytes(b"pdf-data")
    message = InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="Проверь отчет",
        source_message_id="cursor-retry-mid",
        attachments=(
            InboundAttachment(
                filename="report.pdf",
                mime="application/pdf",
                size=8,
                temp_path=str(incoming_path),
            ),
        ),
    )
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        load_state=repo.load,
        save_state=save_state,
        send_with_budget=lambda *_a, **_k: None,
        bridge=SimpleNamespace(broadcast=lambda *_a, **_k: None),
        orchestrator=orchestrator,
    )

    with pytest.raises(OSError, match="cursor store unavailable"):
        handle_runtime_inbound(source="dialogs", cursor=41, message=message, ctx=ctx)

    assert incoming_path.exists()
    assert repo.load().provider_state["dialogs"]["seq"] == 40
    assert handle_runtime_inbound(source="dialogs", cursor=41, message=message, ctx=ctx)

    provider = create_storage_provider(data_dir=tmp_path)
    assert not incoming_path.exists()
    assert len(store.list_tasks()) == 1
    assert len(list_owner_messages(tmp_path)) == 1
    assert len(provider.list_keys(SCOPE_UPLOADS, "chat/")) == 1
    assert len(provider.list_keys(SCOPE_UPLOADS, "_meta/chat/")) == 1
    assert repo.load().provider_state["dialogs"]["seq"] == 41


def test_dialogs_input_loop_retries_pending_message_before_advancing_cursor(monkeypatch):
    from supervisor import server_runtime

    message = InboundMessage(chat_id="peer-42", user_id="user-42", text="retry me")

    class Provider:
        def __init__(self):
            self.recv_calls = 0

        def recv_update(self, cursor, timeout_sec=0):
            self.recv_calls += 1
            if self.recv_calls == 1:
                return message, 7
            return None, cursor

    provider = Provider()
    transport = SimpleNamespace(
        settings=_dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        ),
        provider=provider,
        provider_active=True,
        _provider_lock=threading.Lock(),
    )
    cursor_ref = {"value": 0}
    stop_event = threading.Event()
    processed = threading.Event()
    attempts = []

    def handle(**kwargs):
        attempts.append(kwargs["message"])
        if len(attempts) == 1:
            raise OSError("temporary persistence failure")
        processed.set()
        stop_event.set()
        return True

    monkeypatch.setattr(server_runtime, "handle_runtime_inbound", handle)
    thread = server_runtime.start_dialogs_runtime_input_thread(
        runtime_transport=transport,
        dialogs_cursor_ref=cursor_ref,
        ctx=SimpleNamespace(),
        logger=SimpleNamespace(warning=lambda *_a, **_k: None, info=lambda *_a, **_k: None),
        stop_event=stop_event,
    )

    assert processed.wait(2.0)
    thread.join(timeout=2.0)
    assert attempts == [message, message]
    assert provider.recv_calls == 1
    assert cursor_ref["value"] == 7


def test_dialogs_input_loop_logs_full_failure_traceback(caplog, monkeypatch):
    from supervisor import server_runtime

    message = InboundMessage(chat_id="peer-42", user_id="user-42", text="boom")

    class Provider:
        def recv_update(self, cursor, timeout_sec=0):
            return message, 3

    transport = SimpleNamespace(
        settings=_dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpc://dialogs",
            DIALOGS_BOT_TOKEN="token",
        ),
        provider=Provider(),
        provider_active=True,
        _provider_lock=threading.Lock(),
    )
    stop_event = threading.Event()
    attempts = []

    def handle(**kwargs):
        attempts.append(kwargs["message"])
        if len(attempts) == 1:
            raise ValueError("chat encryption key is not configured")
        stop_event.set()
        return True

    monkeypatch.setattr(server_runtime, "handle_runtime_inbound", handle)
    with caplog.at_level(logging.WARNING, logger="supervisor.runtime"):
        thread = server_runtime.start_dialogs_runtime_input_thread(
            runtime_transport=transport,
            dialogs_cursor_ref={"value": 0},
            ctx=SimpleNamespace(),
            logger=logging.getLogger("supervisor.runtime"),
            stop_event=stop_event,
        )
        assert stop_event.wait(2.0)
        thread.join(timeout=2.0)

    failures = [record for record in caplog.records if "Dialogs input loop failed" in record.getMessage()]
    assert failures, caplog.text
    record = failures[0]
    assert "exception_type=ValueError" in record.getMessage()
    assert "error=chat encryption key is not configured" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is ValueError
    formatted = "".join(traceback.format_exception(*record.exc_info))
    assert "ValueError: chat encryption key is not configured" in formatted
    assert "raise ValueError" in formatted
    assert attempts == [message, message]


def test_handle_runtime_inbound_rolls_back_earlier_files_when_later_storage_fails(
    monkeypatch,
    tmp_path,
):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from supervisor import server_runtime

    _repo, ctx, _captured = _runtime_context(tmp_path)
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    real_store = server_runtime.store_attachment_path
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("storage unavailable")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(server_runtime, "store_attachment_path", fail_second)
    with pytest.raises(OSError, match="storage unavailable"):
        server_runtime.handle_runtime_inbound(
            source="dialogs",
            cursor=18,
            message=InboundMessage(
                chat_id="peer-42",
                user_id="user-42",
                text="Два файла",
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
            ),
            ctx=ctx,
        )

    provider = create_storage_provider(data_dir=tmp_path)
    assert provider.list_keys(SCOPE_UPLOADS, "chat/") == []
    assert provider.list_keys(SCOPE_UPLOADS, "_meta/chat/") == []


@pytest.mark.parametrize(
    "group_id,peer_id,peer_type",
    [
        pytest.param("peer-42", "other-peer", 2, id="different-group-id"),
        pytest.param("42", "42", 1, id="non-group-peer-type"),
    ],
)
def test_dialogs_provider_filters_configured_group(group_id, peer_id, peer_type):
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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


def test_dialogs_conversation_key_identifies_the_chat():
    group_inbound, _reason = _identity_provider()._normalize_sdk_update_message(_group_update("hi", user_id="7"))
    private_inbound, _reason = _sdk_provider(SimpleNamespace())._normalize_sdk_update_message(
        _dialogs_text_update("hi", user_id="7")
    )

    assert group_inbound.conversation_id == "dialogs:42"
    assert private_inbound.conversation_id == "dialogs:peer-42"


def test_dispatch_persists_sender_label_and_conversation_id(tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages
    from supervisor.orchestrator.intent import TaskIntentKind
    from supervisor.server_runtime import _dispatch_runtime_inbound

    envelopes = []
    handled = _dispatch_runtime_inbound(
        message=InboundMessage(
            chat_id="42",
            user_id="7",
            text="привет",
            source_message_id="mid-1",
            sender_label="Сотрудник 7",
            conversation_id="dialogs:42",
        ),
        text="привет",
        ctx=SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            orchestrator=SimpleNamespace(
                submit=lambda envelope: (
                    envelopes.append(envelope) or SimpleNamespace(intent=TaskIntentKind.AGENT_CHAT, task_id="task-1")
                )
            ),
        ),
    )

    assert handled is True
    assert (envelopes[0].sender_label, envelopes[0].conversation_id) == ("Сотрудник 7", "dialogs:42")
    persisted = list_owner_messages(tmp_path)[0]
    assert persisted.conversation_id == "dialogs:42"
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
        pytest.param("ouroboros_bot", "почисти историю", "почисти историю", False, id="clean-history-is-speech"),
        pytest.param(
            "ouroboros_bot", "@ouroboros_bot почисти историю", "почисти историю", True, id="clean-history-addressed"
        ),
        pytest.param("ouroboros_bot", "/deploy prod", "/deploy prod", False, id="another-bots-command"),
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


@pytest.mark.parametrize(
    ("messaging_method", "send"),
    [
        pytest.param("send_message_sync", lambda provider: provider.send_text("peer-42", "мой ответ"), id="text"),
        pytest.param(
            "send_file_sync",
            lambda provider: provider.send_file("peer-42", b"report body", filename="report.txt"),
            id="file",
        ),
    ],
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

    with caplog.at_level(logging.WARNING, logger="supervisor.messaging.dialogs.inbound"):
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
    from supervisor.messaging.dialogs import inbound as dialogs_inbound

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
    from supervisor.messaging.dialogs import DialogsProvider

    bot_config = DialogsProvider(
        _dialogs_settings(
            DIALOGS_GRPC_ENDPOINT="grpcs://dialogs.internal:443",
            DIALOGS_BOT_TOKEN="token",
        )
    )._build_bot_config()

    assert "root_certificates" not in bot_config


def test_dialogs_provider_builds_bot_config_with_root_certificates(tmp_path):
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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


def test_dialogs_provider_rejects_declared_oversize_before_download():
    from ouroboros.storage.chat_attachments import CHAT_ATTACHMENT_MAX_BYTES

    downloads = []
    provider = _download_provider(lambda *args: downloads.append(args))
    notices = []
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")
    update = _dialogs_file_update(
        message_id="mid-large",
        filename="large.pdf",
        mime="application/pdf",
        size=CHAT_ATTACHMENT_MAX_BYTES + 1,
    )

    provider._on_file_passed(update)

    assert downloads == []
    assert provider._incoming.empty()
    assert notices == [
        (
            "peer-42",
            "Файл «large.pdf» отклонён: размер превышает 50 МБ.",
            {"reply_to": ["mid-large"], "target_uid": "user-42"},
        )
    ]


def test_dialogs_provider_reports_actual_download_size_overflow(monkeypatch):
    from supervisor.messaging.dialogs import inbound as provider_module

    monkeypatch.setattr(provider_module, "MAX_IMAGE_BYTES", 8)

    def download_file(path, _document):
        Path(path).write_bytes(b"123456789")

    notices = []
    provider = _download_provider(download_file)
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")

    provider._on_file_passed(_dialogs_file_update(filename="large.png", mime="image/png"))
    provider._file_jobs.join()

    assert provider._incoming.empty()
    assert notices == [
        (
            "peer-42",
            "Файл «large.png» отклонён: размер превышает 20 МБ.",
            {"reply_to": ["mid-1"], "target_uid": "user-42"},
        )
    ]


def test_dialogs_file_download_logs_safe_milestones_and_error(caplog, monkeypatch):
    from ouroboros.storage.chat_attachments import AttachmentError
    from supervisor.messaging.dialogs import inbound as inbound_module

    provider = _download_provider(lambda path, _document: Path(path).write_text("report", encoding="utf-8"))
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

    def fail(_path, _document):
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
    provider = _download_provider(lambda *_args: None)
    provider._bot.internal.downloading.download_file_sync = None

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        provider._process_file_passed(_dialogs_file_update(message_id="mid-missing"), attempt=2)

    assert "event=dialogs_file_download_started filename=report.txt message_id=mid-missing attempt=2" in caplog.text
    assert "event=dialogs_file_download_failed filename=report.txt message_id=mid-missing attempt=2" in caplog.text
    assert "stage=download_file_sync exception_type=RuntimeError message=download_failed duration=" in caplog.text


def test_dialogs_download_setup_failure_releases_capacity(monkeypatch, tmp_path):
    from supervisor.messaging.dialogs import inbound as inbound_module

    provider = _download_provider(lambda *_args: None)
    monkeypatch.setattr(
        inbound_module.os,
        "mkfifo",
        lambda *_args: (_ for _ in ()).throw(OSError("fifo unavailable")),
    )

    with pytest.raises(OSError, match="fifo unavailable"):
        provider._download_document(lambda *_args: None, tmp_path / "report.txt", object(), 1024)

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

    def download(path, _document):
        Path(path).write_bytes(payload)

    provider = _download_provider(download)
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")

    provider._on_file_passed(_dialogs_file_update(filename=filename, mime=mime))
    provider._file_jobs.join()

    assert provider._incoming.empty()
    assert len(notices) == 1
    assert filename in notices[0][1]


def test_dialogs_shutdown_prevents_late_file_enqueue_and_removes_temp(monkeypatch):
    from supervisor.messaging.dialogs import inbound as provider_module

    started = threading.Event()
    finish = threading.Event()
    download_paths = []

    def download(path, _document):
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
    from supervisor.messaging.dialogs import DialogsProvider

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


def test_dialogs_file_callback_does_not_block_on_download():
    download_started = threading.Event()
    allow_download = threading.Event()

    def download_file(path, _document):
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

    from supervisor.messaging.dialogs import DialogsProvider

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
    assert provider._file_jobs.get_nowait() is update


def test_dialogs_provider_close_stops_file_workers():
    from supervisor.messaging.dialogs import DialogsProvider

    provider = DialogsProvider(_dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpc://dialogs", DIALOGS_BOT_TOKEN="token"))
    provider._ensure_file_workers()
    workers = list(provider._file_workers)

    provider.close()

    assert workers
    assert all(not worker.is_alive() for worker in workers)


def test_dialogs_runtime_close_waits_for_input_and_rejects_late_message(monkeypatch):
    from supervisor import server_runtime
    from supervisor.server_runtime import DialogsRuntimeTransport, start_dialogs_runtime_input_thread

    entered = threading.Event()
    release = threading.Event()
    handled = []

    class Provider:
        def recv_update(self, _cursor, timeout_sec=0):
            entered.set()
            assert release.wait(2)
            return InboundMessage(chat_id="peer-42", user_id="user-42", text="late"), 1

        def close(self):
            handled.append("closed")

    transport = DialogsRuntimeTransport(
        _dialogs_settings(DIALOGS_GRPC_ENDPOINT="grpc://dialogs", DIALOGS_BOT_TOKEN="token"),
        _DummyBridge(),
        None,
        provider=Provider(),
    )
    monkeypatch.setattr(
        server_runtime,
        "handle_runtime_inbound",
        lambda **_kwargs: handled.append("message") or True,
    )
    transport.dialogs_input_thread = start_dialogs_runtime_input_thread(
        runtime_transport=transport,
        dialogs_cursor_ref={"value": 0},
        ctx=SimpleNamespace(),
        logger=None,
        stop_event=threading.Event(),
    )
    assert entered.wait(1)

    closer = threading.Thread(target=transport.close)
    try:
        closer.start()
        closer.join(timeout=0.1)
        assert closer.is_alive()
    finally:
        release.set()
        closer.join(timeout=1)

    assert not closer.is_alive()
    assert handled == ["closed"]


@pytest.mark.parametrize("permanent_failure", [False, True])
def test_dialogs_provider_retries_dlp_file_after_download_failure(permanent_failure):
    attempts = 0

    def download_file(path, _document):
        nonlocal attempts
        attempts += 1
        if permanent_failure or attempts == 1:
            raise OSError("temporary download failure")
        Path(path).write_text("report data", encoding="utf-8")

    provider = _download_provider(download_file)
    notices = []
    provider.send_text = lambda chat_id, text, **kwargs: notices.append((chat_id, text, kwargs)) or (True, "ok")
    update = _dialogs_file_update(
        message_id="mid-retry",
        mime="text/plain",
    )
    update.message.document_message.caption = "Проверь отчет"

    provider._on_file_passed(update)
    if permanent_failure:
        provider._file_jobs.join()
        assert attempts > 1
        assert provider._incoming.empty()
        assert notices and "report.txt" in notices[0][1]
        return
    inbound = provider._incoming.get(timeout=2.0)

    assert attempts == 2
    assert notices == []
    assert inbound.text == "Проверь отчет"
    assert inbound.source_message_id == "mid-retry"
    assert inbound.attachments[0].filename == "report.txt"
    assert inbound.attachments[0].mime == "text/plain"
    assert Path(inbound.attachments[0].temp_path).read_text(encoding="utf-8") == "report data"
    Path(inbound.attachments[0].temp_path).unlink()


def test_dialogs_provider_rejects_dlp_blocked_file_without_queueing():
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
    from supervisor.messaging.dialogs import DialogsProvider

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
        append_jsonl=lambda *_a, **_k: None,
        DRIVE_ROOT=".",
    )

    events._handle_send_message(
        {"chat_id": "peer-42", "text": "hello", "task_id": "task-1"},
        ctx,
    )

    assert calls[0][0][0] == "peer-42"


def test_dialogs_slash_response_preserves_origin_transport(tmp_path, monkeypatch):
    from supervisor import commands
    from supervisor.orchestrator.intent import TaskIntentKind
    from supervisor.server_runtime import _dispatch_runtime_inbound

    monkeypatch.setattr(commands, "status_text", lambda *_args: "status")
    calls = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        orchestrator=SimpleNamespace(
            submit=lambda _envelope: SimpleNamespace(intent=TaskIntentKind.SLASH_COMMAND, slash_command="/status")
        ),
        executor=SimpleNamespace(worker_slots_dict=dict),
        soft_timeout=600,
        hard_timeout=1800,
        send_with_budget=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    handled = _dispatch_runtime_inbound(
        message=InboundMessage(
            chat_id="group-42",
            user_id="alice",
            text="/status",
            source_message_id="mid-1",
        ),
        text="/status",
        ctx=ctx,
    )

    assert handled is True
    assert calls[-1][1]["source"] == "dialogs"
    assert calls[-1][1]["transport"] == {
        "source_message_id": "mid-1",
        "target_uid": "alice",
    }


def test_dialogs_slash_retry_does_not_redeliver_response(tmp_path, monkeypatch):
    from supervisor import commands
    from supervisor.orchestrator.intent import TaskIntentKind
    from supervisor.server_runtime import _dispatch_runtime_inbound

    monkeypatch.setattr(commands, "status_text", lambda *_args: "status")
    calls = []

    def send_with_budget(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("local projection failed after external delivery")

    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        orchestrator=SimpleNamespace(
            submit=lambda _envelope: SimpleNamespace(intent=TaskIntentKind.SLASH_COMMAND, slash_command="/status")
        ),
        executor=SimpleNamespace(worker_slots_dict=dict),
        soft_timeout=600,
        hard_timeout=1800,
        send_with_budget=send_with_budget,
    )
    message = InboundMessage(
        chat_id="group-42",
        user_id="alice",
        text="/status",
        source_message_id="mid-status-1",
    )

    with pytest.raises(OSError, match="local projection failed"):
        _dispatch_runtime_inbound(message=message, text=message.text, ctx=ctx)

    assert _dispatch_runtime_inbound(message=message, text=message.text, ctx=ctx) is True
    assert len(calls) == 1


def test_local_chat_bridge_send_message_accepts_string_chat_id(monkeypatch):
    import supervisor.message_bus as message_bus

    monkeypatch.setattr(message_bus.LocalChatBridge, "_restart_telegram_polling", lambda self: None)
    bridge = message_bus.LocalChatBridge({})
    broadcasts = []
    bridge.set_browser_sink(broadcasts.append)

    ok, status = bridge.send_message("peer-42", "hello")

    assert (ok, status) == (True, "ok")
    assert broadcasts == []


def test_local_chat_bridge_broadcasts_only_browser_source():
    import supervisor.message_bus as message_bus

    bridge = message_bus.LocalChatBridge({})
    broadcasts = []
    bridge.set_browser_sink(broadcasts.append)

    bridge.send_message(1, "dialogs reply", source="dialogs")
    bridge.send_message(1, "browser reply", source="browser")

    assert [payload["content"] for payload in broadcasts] == ["browser reply"]


def test_dispatch_runtime_inbound_handles_clean_command(tmp_path, monkeypatch):

    import supervisor.message_bus as message_bus
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.server_runtime import _dispatch_runtime_inbound

    store = init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="old",
        source="dialogs",
    )
    assert list_owner_messages(tmp_path)
    messages = []
    broadcasts = []

    monkeypatch.setattr(
        message_bus,
        "log_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("clean command must not be logged")),
    )

    handled = _dispatch_runtime_inbound(
        message=InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="/clean",
            source_message_id="mid-clean",
        ),
        text="/clean",
        ctx=SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            store=store,
            send_with_budget=lambda chat_id, text, **kwargs: messages.append((chat_id, text, kwargs)),
            bridge=SimpleNamespace(broadcast=lambda payload: broadcasts.append(payload)),
        ),
    )

    assert handled is True
    assert messages == [
        (
            "peer-42",
            "История очищена",
            {
                "source": "dialogs",
                "transport": {"source_message_id": "mid-clean", "target_uid": "user-42"},
            },
        )
    ]
    assert broadcasts and broadcasts[-1]["type"] == "chat_history_cleared"
    remaining = list_owner_messages(tmp_path)
    assert [message.content for message in remaining] == ["/clean"]
    assert remaining[0].metadata["inbound_processed"] is True


def test_clean_command_with_attachment_is_rejected_atomically(tmp_path):
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.storage.chat_attachments import get_attachment, store_attachment_bytes
    from ouroboros.storage.chat_store import list_owner_messages
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.server_runtime import _dispatch_runtime_inbound

    provider = create_storage_provider(data_dir=tmp_path)
    metadata = store_attachment_bytes(
        provider,
        attachment_id="attachment-clean",
        filename="note.txt",
        mime="text/plain",
        data=b"hello",
        source="dialogs",
        status="ready",
        message_id="mid-clean",
    )
    messages = []

    store = init_runtime_store(tmp_path)
    handled = _dispatch_runtime_inbound(
        message=InboundMessage(
            chat_id="peer-42",
            user_id="user-42",
            text="/clean",
            source_message_id="mid-clean",
        ),
        text="/clean",
        ctx=SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            store=store,
            send_with_budget=lambda _chat_id, text, **_kwargs: messages.append(text),
            bridge=SimpleNamespace(broadcast=lambda _payload: None),
        ),
        attachment_ids=["attachment-clean"],
        attachments=[metadata],
    )

    assert handled is True
    assert messages == ["Команды нельзя отправлять вместе с файлами: note.txt"]
    assert provider.get_bytes(SCOPE_UPLOADS, metadata["storage_key"]) is None
    assert get_attachment(provider, "attachment-clean") is None
    assert list_owner_messages(tmp_path) == []


def test_history_only_clean_is_persisted_without_execution(tmp_path):
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.server_runtime import _dispatch_runtime_inbound

    store = init_runtime_store(tmp_path)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="old",
        source="dialogs",
    )
    replies = []
    message = InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="/clean",
        source_message_id="history-clean",
        history_only=True,
    )

    assert _dispatch_runtime_inbound(
        message=message,
        text=message.text,
        ctx=SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            store=store,
            send_with_budget=lambda *_args, **_kwargs: replies.append("sent"),
            bridge=SimpleNamespace(broadcast=lambda _payload: None),
        ),
    )

    persisted = list_owner_messages(tmp_path)
    assert [item.content for item in persisted] == ["old", "/clean"]
    assert persisted[-1].metadata["inbound_processed"] is True
    assert replies == []


@pytest.mark.parametrize("status", ["queued", "scheduled", "interrupted", "running"])
def test_clean_is_rejected_while_any_system_task_is_active(tmp_path, status):
    from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.server_runtime import _dispatch_runtime_inbound

    store = init_runtime_store(tmp_path)
    store.create_task(TaskRequest(id=f"task-{status}", chat_id="another-chat", text="work"), status=status)
    append_chat_message(
        tmp_path,
        actor_type="owner",
        chat_id=1,
        user_id=1,
        text="old",
        source="browser",
    )
    replies = []
    message = InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="/clean",
        source_message_id=f"clean-{status}",
    )

    assert _dispatch_runtime_inbound(
        message=message,
        text=message.text,
        ctx=SimpleNamespace(
            DRIVE_ROOT=tmp_path,
            store=store,
            send_with_budget=lambda _chat_id, text, **_kwargs: replies.append(text),
            bridge=SimpleNamespace(broadcast=lambda _payload: None),
        ),
    )

    messages = list_owner_messages(tmp_path)
    assert [item.content for item in messages] == ["old", "/clean"]
    assert messages[-1].metadata["inbound_processed"] is True
    assert replies == ["Нельзя очистить историю, пока есть активные задачи."]


def test_processed_clean_replay_is_noop(tmp_path):
    from ouroboros.storage.chat_store import list_owner_messages
    from ouroboros.storage.runtime_store import init_runtime_store
    from supervisor.server_runtime import _dispatch_runtime_inbound

    store = init_runtime_store(tmp_path)
    replies = []
    message = InboundMessage(
        chat_id="peer-42",
        user_id="user-42",
        text="/clean",
        source_message_id="clean-replay",
    )
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        store=store,
        send_with_budget=lambda _chat_id, text, **_kwargs: replies.append(text),
        bridge=SimpleNamespace(broadcast=lambda _payload: None),
    )

    assert _dispatch_runtime_inbound(message=message, text=message.text, ctx=ctx)
    assert _dispatch_runtime_inbound(message=message, text=message.text, ctx=ctx)

    assert replies == ["История очищена"]
    assert [item.content for item in list_owner_messages(tmp_path)] == ["/clean"]
