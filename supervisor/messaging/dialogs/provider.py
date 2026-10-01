from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Any

from ouroboros.storage.settings.schema import AppSettings
from supervisor.messaging.base import InboundMessage
from supervisor.messaging.dialogs.inbound import (
    _DIALOGS_DOWNLOAD_TIMEOUT_SEC,
    _DIALOGS_FILE_QUEUE_SIZE,
    _DIALOGS_FILE_WORKERS,
    _DIALOGS_INCOMING_QUEUE_SIZE,
    DialogsInbound,
)
from supervisor.messaging.dialogs.outbound import DialogsOutbound
from supervisor.messaging.dialogs.state import SENT_MESSAGE_ID_MEMORY, DialogsPeer, DialogsSenderPeer

log = logging.getLogger(__name__)

__all__ = [
    "DialogsProvider",
    "_dialogs_bot_token",
]


def _dialogs_bot_token(settings: AppSettings) -> str:
    return str(settings.DIALOGS_BOT_TOKEN.get_secret_value() or "").strip()


class DialogsProvider(DialogsInbound, DialogsOutbound):
    """Dialogs SDK lifecycle and peer routing."""

    name = "dialogs"

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._bot = None
        self._group_peer: DialogsPeer | None = None
        self._incoming: queue.Queue[InboundMessage] = queue.Queue(maxsize=_DIALOGS_INCOMING_QUEUE_SIZE)
        self._incoming_lock = threading.Lock()
        self._cursor = 0
        self._bot_lock = threading.RLock()
        self._handlers_registered = False
        self._updates_thread: threading.Thread | None = None
        self._updates_stop_event = threading.Event()
        self._last_updates_error = ""
        self._out_peer_by_chat_id: dict[str, DialogsPeer] = {}
        self._seen_file_updates: set[str] = set()
        self._seen_file_update_order: deque[str] = deque()
        self._history_recovery_lock = threading.Lock()
        self._history_recovery_state: dict[str, str] = {}
        self._history_recovery_failed_at: dict[str, float] = {}
        self._sender_labels: dict[str, str] = {}
        self._sender_label_retry_at: dict[str, float] = {}
        self._progress_messages: dict[str, Any] = {}
        self._progress_fallbacks: set[str] = set()
        self._sent_message_ids: deque[str] = deque(maxlen=SENT_MESSAGE_ID_MEMORY)
        self._foreign_message_ids: deque[str] = deque(maxlen=SENT_MESSAGE_ID_MEMORY)
        self._file_jobs: queue.Queue[Any] = queue.Queue(maxsize=_DIALOGS_FILE_QUEUE_SIZE)
        self._file_workers: list[threading.Thread] = []
        self._file_workers_lock = threading.Lock()
        self._download_slots = threading.BoundedSemaphore(_DIALOGS_FILE_WORKERS)
        self._outbound_slots = threading.BoundedSemaphore(4)
        self._lookup_slots = threading.BoundedSemaphore(2)
        self._reply_lookup_warned = False

    def connect(self) -> None:
        self._ensure_bot()

    def authenticate(self) -> None:
        self._ensure_bot()
        self._register_handlers()
        if self._group_filter_enabled():
            peer = self._resolve_group_peer()
            if peer is None:
                log.warning(
                    "Dialogs provider group peer unresolved at startup group_id=%s",
                    self._group_id(),
                )
            else:
                self._recover_peer_history_once(peer)
        self._ensure_updates_thread()

    def close(self) -> None:
        with self._incoming_lock:
            self._updates_stop_event.set()
        workers = [*self._file_workers]
        if self._updates_thread is not None:
            workers.append(self._updates_thread)
        for worker in workers:
            if worker is not threading.current_thread():
                timeout = _DIALOGS_DOWNLOAD_TIMEOUT_SEC + 1.0 if worker in self._file_workers else 1.0
                worker.join(timeout=timeout)
        while True:
            try:
                inbound = self._incoming.get_nowait()
            except queue.Empty:
                break
            try:
                for attachment in inbound.attachments:
                    Path(attachment.temp_path).unlink(missing_ok=True)
            finally:
                self._incoming.task_done()

    def _ensure_bot(self):
        if self._bot is not None:
            return self._bot

        with self._bot_lock:
            if self._bot is not None:
                return self._bot

            try:
                from dialog_bot_sdk.bot import DialogBot
            except Exception as exc:
                log.error("Dialogs provider failed to import dialog-bot-sdk", exc_info=True)
                raise RuntimeError("dialog-bot-sdk is not installed; install the dialog-bot-sdk package") from exc

            bot_config = self._build_bot_config()
            self._bot = self._create_sdk_bot(DialogBot, bot_config)
            return self._bot

    def _create_sdk_bot(self, dialog_bot_cls: Any, bot_config: dict[str, Any]):
        create_bot = getattr(dialog_bot_cls, "create_bot", None)
        if callable(create_bot):
            return create_bot(bot_config)

        endpoint = str(bot_config.get("endpoint") or "")
        token = str(bot_config.get("token") or "")
        is_secure = bool(bot_config.get("is_secure"))
        if is_secure:
            secure_factory = getattr(dialog_bot_cls, "get_secure_bot", None)
            if callable(secure_factory):
                try:
                    import grpc
                except Exception as exc:
                    raise RuntimeError("grpc is required by dialog-bot-sdk get_secure_bot") from exc
                root_certificates = self._read_root_certificates_bytes(
                    bot_config.get("root_certificates"),
                )
                credentials = grpc.ssl_channel_credentials(root_certificates=root_certificates)
                return secure_factory(endpoint, credentials, token)

        for method_name in ("get_insecure_bot", "get_unsecure_bot"):
            factory = getattr(dialog_bot_cls, method_name, None)
            if callable(factory):
                return factory(endpoint, token)

        raise RuntimeError("dialog-bot-sdk DialogBot factory method is not available")

    def _read_root_certificates_bytes(self, path_value: Any) -> bytes | None:
        path_text = str(path_value or "").strip()
        if not path_text:
            return None
        try:
            return Path(path_text).expanduser().read_bytes()
        except Exception as exc:
            raise RuntimeError(f"failed to read Dialogs root certificates: {path_text}") from exc

    def _build_bot_config(self) -> dict[str, Any]:
        endpoint, is_secure = self._sdk_endpoint_and_security(
            self.settings.DIALOGS_GRPC_ENDPOINT,
            self.settings.DIALOGS_SECURE,
        )
        config: dict[str, Any] = {
            "endpoint": endpoint,
            "token": _dialogs_bot_token(self.settings),
            "is_secure": bool(is_secure),
        }
        if not is_secure:
            config["requests_url_replace"] = {
                "protocol": "http",
                "port": int(self.settings.DIALOGS_FILE_URL_PORT),
            }
        root_certificates = self._root_certificates_path()
        if root_certificates:
            config["root_certificates"] = root_certificates
        return config

    def _startup_config_summary(self) -> str:
        endpoint, is_secure = self._sdk_endpoint_and_security(
            self.settings.DIALOGS_GRPC_ENDPOINT,
            self.settings.DIALOGS_SECURE,
        )
        token_configured = bool(_dialogs_bot_token(self.settings))
        group_id = str(self.settings.DIALOGS_GROUP_ID or "").strip()
        user_id = str(self.settings.DIALOGS_USER_ID or "").strip()
        return (
            f"endpoint={endpoint or 'missing'} "
            f"token={'set' if token_configured else 'missing'} "
            f"secure={bool(is_secure)} "
            f"group_id={'set' if group_id else 'unset'} "
            f"user_id={'set' if user_id else 'unset'}"
        )

    def _sdk_endpoint_and_security(self, endpoint: str, is_secure: bool) -> tuple[str, bool]:
        value = str(endpoint or "").strip()
        if not value:
            return "epbotsift.sberchat.sberbank.ru:443", bool(is_secure)
        if "://" in value:
            value = value.split("://", 1)[1]
        return value, bool(is_secure)

    def _root_certificates_path(self) -> str:
        raw = str(self.settings.DIALOGS_ROOT_CERTIFICATES or "").strip()
        if not raw:
            return ""
        return str(Path(raw).expanduser())

    def _resolve_out_peer(self, chat_id: Any) -> DialogsPeer | None:
        if hasattr(chat_id, "id"):
            return chat_id
        key = "" if chat_id in (None, "") else str(chat_id).strip()
        if not key:
            return None
        peer = self._out_peer_by_chat_id.get(key)
        if peer is not None:
            return peer
        if self._group_filter_enabled() and key == str(self._group_id()):
            return self._resolve_group_peer()
        return None

    def _resolve_group_peer(self) -> DialogsPeer | None:
        if not self._group_filter_enabled():
            return None
        if self._group_peer is not None:
            return self._group_peer
        if self._bot is None:
            return None

        group_id = self._group_id()
        if group_id is None:
            return None
        groups = getattr(self._bot, "groups", None)
        finder = getattr(groups, "find_group_by_id_sync", None)
        if callable(finder):
            try:
                group = finder(group_id)
                peer = getattr(group, "peer", None)
                if peer is not None:
                    self._remember_group_peer(peer)
                    return peer
            except Exception:
                log.warning("Dialogs SDK failed to resolve group %s", group_id, exc_info=True)

        peer = self._build_group_peer(group_id)
        if peer is not None:
            self._remember_group_peer(peer)
        return peer

    def _remember_out_peer(self, chat_id: str, peer: DialogsPeer) -> None:
        if chat_id:
            self._out_peer_by_chat_id[str(chat_id)] = peer

    def _remember_group_peer(self, peer: DialogsPeer) -> None:
        peer_id = self._peer_id(peer)
        group_id = self._group_id()
        if group_id is None or peer_id != group_id:
            return
        self._group_peer = peer
        self._remember_out_peer(str(peer_id), peer)

    def _group_filter_enabled(self) -> bool:
        return bool(str(self.settings.DIALOGS_GROUP_ID or "").strip())

    def _user_filter_enabled(self) -> bool:
        return bool(self._configured_user_key())

    def _configured_user_key(self) -> str:
        return str(self.settings.DIALOGS_USER_ID or "").strip()

    def _is_configured_user_sender(self, sender_peer: DialogsSenderPeer | None) -> bool:
        user_id = self._configured_user_key()
        if not user_id:
            return True
        sender_id = self._sender_key(sender_peer)
        return bool(sender_id and sender_id == user_id)

    def _is_configured_group_peer(self, peer: DialogsPeer | None) -> bool:
        peer_id = self._peer_id(peer)
        group_id = self._group_id()
        if group_id is None or peer_id is None or peer_id != group_id:
            return False

        peer_type = self._peer_type(peer)
        if peer_type is None:
            return True

        try:
            from dialog_bot_sdk.entities.peers import PeerType

            group_type = int(PeerType.PEERTYPE_GROUP)
        except Exception:
            group_type = 2
        return int(peer_type) == group_type

    def _is_group_peer(self, peer: DialogsPeer | None) -> bool:
        peer_type = self._peer_type(peer)
        if peer_type is not None:
            return peer_type == self._as_int_or_none(self._sdk_group_peer_type())
        return self._group_filter_enabled() and self._is_configured_group_peer(peer)

    def _is_self_sender(self, sender_peer: DialogsSenderPeer | None) -> bool:
        sender_id = self._sender_key(sender_peer)
        if not sender_id or self._bot is None:
            return False
        self_id = self._self_sender_key()
        return bool(self_id and sender_id == self_id)

    def _self_sender_key(self) -> str:
        user_info = getattr(self._bot, "user_info", None)
        user = getattr(user_info, "user", None)
        return self._sender_key(getattr(user, "peer", None))

    def _extract_text(self, update: Any) -> str:
        message = getattr(update, "message", None)
        if message is None:
            return ""

        for attr in ("text_message", "textMessage"):
            text_message = getattr(message, attr, None)
            text = getattr(text_message, "text", None)
            if text is not None:
                return str(text or "")

        if isinstance(message, dict):
            text_message = message.get("text_message") or message.get("textMessage")
            if isinstance(text_message, dict):
                return str(text_message.get("text") or "")
            return str(message.get("text") or "")

        return ""

    def _peer_key(self, peer: DialogsPeer | None) -> str:
        value = getattr(peer, "id", None)
        return str(value).strip() if value not in (None, "") else ""

    def _group_id(self) -> int | None:
        raw = self.settings.DIALOGS_GROUP_ID
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _peer_id(self, peer: DialogsPeer | None) -> int | None:
        return self._as_int_or_none(getattr(peer, "id", None))

    def _sender_key(self, peer: DialogsSenderPeer | None) -> str:
        value = getattr(peer, "id", None)
        if value is None:
            value = getattr(peer, "uid", None)
        return str(value).strip() if value not in (None, "") else ""

    def _peer_type(self, peer: DialogsPeer | None) -> int | None:
        return self._as_int_or_none(getattr(peer, "type", None))

    def _build_group_peer(self, group_id: int) -> DialogsPeer | None:
        peer_type = self._sdk_group_peer_type()
        for module_name in (
            "dialog_bot_sdk.entities.peers",
            "dialog_bot_sdk.entities.messaging",
        ):
            try:
                module = __import__(module_name, fromlist=["Peer"])
                peer_cls = module.Peer
            except Exception:
                continue

            # Positional order follows the SDK signature Peer(id, type).
            for args, kwargs in (
                ((), {"type": peer_type, "id": group_id}),
                ((group_id, peer_type), {}),
            ):
                try:
                    return peer_cls(*args, **kwargs)
                except TypeError:
                    continue
        return None

    def _sdk_group_peer_type(self):
        try:
            from dialog_bot_sdk.entities.peers import PeerType

            return PeerType.PEERTYPE_GROUP
        except Exception:
            return 2

    @staticmethod
    def _as_int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None
