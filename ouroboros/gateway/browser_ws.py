"""Browser WebSocket /ws handler."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from ouroboros.app.config import DATA_DIR
from ouroboros.app.server_runtime import ws_heartbeat_loop
from ouroboros.gateway.contracts import WS_MESSAGE_TYPES
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_bus import SKILL_LIFECYCLE, get_global_event_bus
from ouroboros.policy.safety import check_safety
from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.chat_attachments import bind_attachments, public_attachment, release_attachments
from ouroboros.storage.chat_store import (
    append_chat_message,
    get_chat_message_by_client_id,
    mark_chat_message_processed,
)
from ouroboros.utils import set_log_sink, utc_now_iso
from supervisor.commands import handle_slash_command
from supervisor.message_bus import send_with_budget, try_get_bridge
from supervisor.orchestrator.envelope import InboundEnvelope
from supervisor.orchestrator.intent import TaskIntentKind
from supervisor.orchestrator.orchestrator import SubmissionCommitUnknown
from supervisor.queue import _queue_lock
from supervisor.runtime_commands import is_runtime_command, runtime_slash_command
from supervisor.runtime_context import try_get_runtime

log = logging.getLogger(__name__)

BROWSER_CHAT_ID = 1
_HEARTBEAT_INTERVAL_SEC = 30.0
_MESSAGE_CONFLICT = object()


def _stored_attachments(message: Any) -> tuple[list[dict[str, Any]], list[str]]:
    raw = (message.metadata or {}).get("attachments")
    attachments = [dict(value) for value in raw or [] if isinstance(value, dict)]
    return attachments, [str(value["id"]) for value in attachments if value.get("id")]


class BrowserWebSocketHub:
    """Own browser clients, bridge wiring, and background subscriptions."""

    def __init__(self, *, heartbeat_interval_sec: float | None = None) -> None:
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._clients: list[WebSocket] = []
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lifecycle_sub_id: str | None = None
        self._bridge: Any | None = None

    def has_clients(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def add_client(self, websocket: WebSocket) -> int:
        with self._lock:
            self._clients.append(websocket)
            return len(self._clients)

    def remove_client(self, websocket: WebSocket) -> int:
        with self._lock:
            with suppress(ValueError):
                self._clients.remove(websocket)
            return len(self._clients)

    async def close_all(
        self,
        *,
        code: int = 1012,
        reason: str = "Server restarting",
    ) -> None:
        with self._lock:
            clients = list(self._clients)
        for websocket in clients:
            try:
                await websocket.close(code=code, reason=reason)
            except Exception:
                log.debug("Failed to close browser WebSocket", exc_info=True)

    async def broadcast(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, default=str)
        message_type = str(message.get("type", "unknown"))
        with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for websocket in clients:
            try:
                await websocket.send_text(data)
            except Exception as exc:
                log.info(
                    "WebSocket send failed for msg type=%s; dropping client (%s)",
                    message_type,
                    type(exc).__name__,
                )
                dead.append(websocket)
        if dead:
            with self._lock:
                for websocket in dead:
                    with suppress(ValueError):
                        self._clients.remove(websocket)

    def broadcast_sync(self, message: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
        except RuntimeError:
            log.debug("Browser event loop stopped before broadcast", exc_info=True)

    def attach_bridge(self, bridge: Any) -> None:
        if bridge is self._bridge:
            return
        self._detach_bridge()
        set_sink = getattr(bridge, "set_browser_sink", None)
        if not callable(set_sink):
            raise TypeError("browser bridge must provide set_browser_sink()")
        set_sink(self.broadcast_sync)
        self._bridge = bridge
        set_log_sink(bridge.push_log)

    def attach_runtime_bridge(self) -> None:
        bridge = try_get_bridge()
        if bridge is not None:
            self.attach_bridge(bridge)

    def _detach_bridge(self) -> None:
        if self._bridge is not None:
            set_sink = getattr(self._bridge, "set_browser_sink", None)
            if callable(set_sink):
                set_sink(None)
        self._bridge = None
        set_log_sink(None)

    async def start(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        bridge: Any | None = None,
    ) -> None:
        self._loop = loop
        if bridge is not None:
            self.attach_bridge(bridge)
        else:
            self.attach_runtime_bridge()
        if self._lifecycle_sub_id is None:
            self._lifecycle_sub_id = get_global_event_bus().subscribe(
                "browser-ingress",
                SKILL_LIFECYCLE,
                lambda data: self.broadcast_sync(_lifecycle_payload(data)),
            )
        if self._heartbeat_task is None or self._heartbeat_task.done():
            interval = (
                self._heartbeat_interval_sec if self._heartbeat_interval_sec is not None else _HEARTBEAT_INTERVAL_SEC
            )
            self._heartbeat_task = asyncio.create_task(
                ws_heartbeat_loop(self.has_clients, self.broadcast, interval_sec=interval),
                name="browser-ws-heartbeat",
            )

    async def stop(self) -> None:
        await self.close_all()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._lifecycle_sub_id is not None:
            get_global_event_bus().unsubscribe(self._lifecycle_sub_id)
            self._lifecycle_sub_id = None
        self._detach_bridge()
        with self._lock:
            self._clients.clear()
        self._loop = None

    def reset_for_tests(self) -> None:
        task = self._heartbeat_task
        if task is not None and not task.done():
            task.cancel()
        self._heartbeat_task = None
        if self._lifecycle_sub_id is not None:
            get_global_event_bus().unsubscribe(self._lifecycle_sub_id)
            self._lifecycle_sub_id = None
        self._detach_bridge()
        with self._lock:
            self._clients.clear()
        self._loop = None


_browser_hub = BrowserWebSocketHub()


def get_browser_ws_hub() -> BrowserWebSocketHub:
    return _browser_hub


async def broadcast_ws(msg: dict[str, Any]) -> None:
    await _browser_hub.broadcast(msg)


def reset_browser_ws_state_for_tests() -> None:
    _browser_hub.reset_for_tests()


def attach_browser_ws_bridge(bridge: Any) -> None:
    _browser_hub.attach_bridge(bridge)


def _drive_root_from_websocket(websocket: WebSocket) -> pathlib.Path:
    if hasattr(websocket.app, "state") and hasattr(websocket.app.state, "drive_root"):
        return pathlib.Path(websocket.app.state.drive_root)
    return pathlib.Path(DATA_DIR)


def _browser_trace_id(websocket: WebSocket, message: dict[str, Any]) -> str:
    requested = str(message.get("trace_id") or "")
    if requested:
        return aef_tracing.ensure_trace_id(requested)
    headers = getattr(websocket, "headers", {})
    return aef_tracing.trace_id_from_headers(headers) or aef_tracing.ensure_trace_id()


def _lifecycle_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "extension_lifecycle",
        "skill": str(data.get("skill") or ""),
        "action": str(data.get("action") or data.get("type") or ""),
        "status": str(data.get("status") or ""),
        "reason": str(data.get("reason") or ""),
        "data": dict(data),
    }


async def _ensure_background_tasks() -> None:
    await _browser_hub.start(asyncio.get_running_loop())


async def _send_ws_error(
    websocket: WebSocket,
    message: str,
    *,
    code: str = "invalid_request",
    retryable: bool = False,
) -> None:
    detail = str(message or "invalid request")
    await websocket.send_text(
        json.dumps(
            {
                "type": "error",
                "error": detail,
                "code": code,
                "message": detail,
                "retryable": retryable,
                "ok": False,
            }
        )
    )


def check_operator_command_policy(cmd: str) -> tuple[bool, str]:
    """Policy gate for WS operator commands via ``policy/safety.py``."""
    text = str(cmd or "").strip()
    if not text:
        return False, "empty command"
    if runtime_slash_command(text):
        return True, ""
    if text.startswith("/"):
        return True, ""
    allowed, message = check_safety("run_shell", {"cmd": text})
    if allowed:
        return True, ""
    return False, message or "command blocked by policy"


def _policy_error_text(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "command blocked by policy"
    first_line = text.splitlines()[0].strip()
    if "SAFETY_VIOLATION" in first_line:
        return first_line
    return first_line[:500]


def _touch_owner_activity(state_repo: Any, ts: str) -> None:
    with state_repo.mutate() as state:
        state.last_owner_message_at = ts


@dataclass
class BrowserCommandContext:
    executor: Any
    soft_timeout: int
    hard_timeout: int
    drive_root: pathlib.Path
    send_with_budget: Callable[..., Any]
    safe_restart: Callable[..., tuple[bool, str]]
    request_restart: Callable[[], None]
    store: Any
    orchestrator: Any
    bridge: Any
    load_state: Callable[[], Any]

    @property
    def WORKERS(self) -> dict[int, Any]:
        return {}

    @property
    def DRIVE_ROOT(self) -> pathlib.Path:
        return self.drive_root


def _command_ctx(websocket: WebSocket, runtime: Any) -> BrowserCommandContext:
    app_state = getattr(websocket.app, "state", None)
    request_restart = getattr(app_state, "request_restart", None) if app_state is not None else None
    safe_restart = getattr(app_state, "safe_restart", None) if app_state is not None else None

    def _noop_restart() -> None:
        if callable(request_restart):
            request_restart()

    def _unavailable_restart(**kwargs: Any) -> tuple[bool, str]:
        del kwargs
        return False, "restart unavailable"

    def _send_browser_message(*args: Any, **kwargs: Any) -> None:
        kwargs.pop("force_budget", None)
        kwargs.setdefault("source", "browser")
        send_with_budget(*args, **kwargs)

    return BrowserCommandContext(
        executor=runtime.executor,
        soft_timeout=runtime.soft_timeout,
        hard_timeout=runtime.hard_timeout,
        drive_root=runtime.drive_root,
        send_with_budget=_send_browser_message,
        safe_restart=safe_restart if callable(safe_restart) else _unavailable_restart,
        request_restart=_noop_restart,
        store=runtime.store,
        orchestrator=runtime.orchestrator,
        bridge=runtime.bridge,
        load_state=runtime.state_repo.load,
    )


async def _dispatch_browser_command(
    websocket: WebSocket,
    runtime: Any,
    command: str,
    *,
    sender_session_id: str,
    client_message_id: str,
) -> None:
    from supervisor.messaging.base import InboundMessage
    from supervisor.server_runtime import _dispatch_runtime_inbound

    message = InboundMessage(
        chat_id=BROWSER_CHAT_ID,
        user_id=BROWSER_CHAT_ID,
        text=command,
        raw_command=command,
        source_message_id=client_message_id,
        sender_session_id=sender_session_id,
    )
    await asyncio.to_thread(
        _dispatch_runtime_inbound,
        message=message,
        text=command,
        ctx=_command_ctx(websocket, runtime),
        source="browser",
    )


async def _handle_inbound_command(
    websocket: WebSocket,
    msg: dict[str, Any],
) -> None:
    cmd = str(msg.get("cmd") or "").strip()
    if not cmd:
        return

    allowed, policy_message = check_operator_command_policy(cmd)
    if not allowed:
        await _send_ws_error(
            websocket,
            _policy_error_text(policy_message),
            code="command_not_allowed",
        )
        return

    runtime = try_get_runtime()
    if runtime is None:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "chat",
                    "role": "assistant",
                    "content": "⚠️ System is still initializing. Please wait a moment and try again.",
                    "ts": utc_now_iso(),
                }
            )
        )
        return

    await _dispatch_browser_command(
        websocket,
        runtime,
        cmd,
        sender_session_id=str(msg.get("sender_session_id") or ""),
        client_message_id=str(msg.get("client_message_id") or uuid.uuid4().hex),
    )


async def _handle_inbound_chat(
    websocket: WebSocket,
    msg: dict[str, Any],
) -> None:
    content = str(msg.get("content") or "").strip()
    raw_attachment_ids = msg.get("attachment_ids")
    attachment_ids: list[str] = []
    if "attachment_ids" in msg:
        if not isinstance(raw_attachment_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in raw_attachment_ids
        ):
            await _send_ws_error(
                websocket,
                "attachment_ids must be an array of non-empty strings",
                code="invalid_attachment_ids",
            )
            return
        attachment_ids = [value.strip() for value in raw_attachment_ids]
        if len(set(attachment_ids)) != len(attachment_ids):
            await _send_ws_error(
                websocket,
                "attachment_ids must contain unique values",
                code="invalid_attachment_ids",
            )
            return
    if not content and not attachment_ids:
        return
    if len(attachment_ids) > 10:
        await _send_ws_error(websocket, "too many attachments", code="too_many_attachments")
        return
    if attachment_ids and content.startswith("/"):
        await _send_ws_error(
            websocket,
            "commands cannot include attachments",
            code="command_with_attachments",
        )
        return

    sender_session_id = str(msg.get("sender_session_id") or "")
    client_message_id = str(msg.get("client_message_id") or uuid.uuid4().hex)
    trace_id = _browser_trace_id(websocket, msg)
    ts = utc_now_iso()
    drive_root = _drive_root_from_websocket(websocket)
    runtime = try_get_runtime()
    if runtime is None:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "chat",
                    "role": "assistant",
                    "content": "⚠️ System is still initializing. Please wait a moment and try again.",
                    "ts": utc_now_iso(),
                }
            )
        )
        return

    if not attachment_ids and is_runtime_command(content):
        await _dispatch_browser_command(
            websocket,
            runtime,
            content,
            sender_session_id=sender_session_id,
            client_message_id=client_message_id,
        )
        return

    provider: StorageProvider | None = None
    internal_attachments: list[dict[str, Any]] = []
    persisted = await asyncio.to_thread(
        get_chat_message_by_client_id,
        drive_root,
        actor_type="owner",
        source="browser",
        chat_id=BROWSER_CHAT_ID,
        user_id=BROWSER_CHAT_ID,
        client_message_id=client_message_id,
        sender_session_id=sender_session_id,
    )
    if persisted is not None:
        internal_attachments, existing_ids = _stored_attachments(persisted)
        if persisted.content != content or existing_ids != attachment_ids:
            await _send_ws_error(
                websocket,
                "duplicate client_message_id has different payload",
                code="message_conflict",
            )
            return
        if bool((persisted.metadata or {}).get("inbound_processed")):
            await broadcast_ws(
                {
                    "type": "chat",
                    "role": "user",
                    "content": persisted.content,
                    "ts": persisted.created_at,
                    "source": "browser",
                    "sender_session_id": sender_session_id,
                    "client_message_id": client_message_id,
                    "trace_id": trace_id,
                    "attachments": [public_attachment(value) for value in internal_attachments],
                }
            )
            return
    if attachment_ids:
        try:
            provider = await asyncio.to_thread(create_storage_provider, data_dir=drive_root)
            bound_attachments = await asyncio.to_thread(
                bind_attachments,
                provider,
                attachment_ids,
                message_id=client_message_id,
            )
            internal_attachments = [dict(value) for value in bound_attachments]
        except ValueError as exc:
            await _send_ws_error(websocket, str(exc), code="attachment_bind_failed")
            return
        except Exception:
            log.warning("Failed to bind browser chat attachments", exc_info=True)
            await _send_ws_error(
                websocket,
                "failed to bind attachments",
                code="attachment_storage_unavailable",
                retryable=True,
            )
            return
    public_attachments = [public_attachment(value) for value in internal_attachments]

    state = None
    try:
        state = await asyncio.to_thread(runtime.state_repo.load)
    except Exception:
        log.warning("Failed to load runtime session for browser chat", exc_info=True)
        session_id = ""
    else:
        session_id = str(state.session_id or "")

    envelope = InboundEnvelope(
        chat_id=BROWSER_CHAT_ID,
        user_id=BROWSER_CHAT_ID,
        text=content,
        source="browser",
        sender_session_id=sender_session_id,
        client_message_id=client_message_id,
        trace_id=trace_id,
        attachment_ids=attachment_ids,
        attachments=internal_attachments,
        transport={"source_message_id": client_message_id},
    )
    failure_code = "message_persistence_failed"

    def _commit_message():
        nonlocal failure_code, persisted
        with _queue_lock:
            current = get_chat_message_by_client_id(
                drive_root,
                actor_type="owner",
                source="browser",
                chat_id=BROWSER_CHAT_ID,
                user_id=BROWSER_CHAT_ID,
                client_message_id=client_message_id,
                sender_session_id=sender_session_id,
            )
            persisted = current
            if current is not None:
                _attachments, existing_ids = _stored_attachments(current)
                if current.content != content or existing_ids != attachment_ids:
                    new_ids = [value for value in attachment_ids if value not in existing_ids]
                    if new_ids and provider is not None:
                        release_attachments(provider, new_ids, message_id=client_message_id)
                    return _MESSAGE_CONFLICT
            if persisted is None:
                try:
                    persisted = append_chat_message(
                        drive_root,
                        actor_type="owner",
                        chat_id=BROWSER_CHAT_ID,
                        user_id=BROWSER_CHAT_ID,
                        text=content,
                        session_id=session_id,
                        ts=ts,
                        source="browser",
                        sender_session_id=sender_session_id,
                        client_message_id=client_message_id,
                        extra_metadata={
                            "inbound_processed": False,
                            **({"attachments": internal_attachments} if internal_attachments else {}),
                        },
                    )
                    if persisted is None:
                        raise RuntimeError("message insert conflict")
                except Exception:
                    if attachment_ids and provider is not None:
                        try:
                            release_attachments(
                                provider,
                                attachment_ids,
                                message_id=client_message_id,
                            )
                        except Exception:
                            log.warning(
                                "Failed to roll back browser attachment binding",
                                exc_info=True,
                            )
                    raise

            if bool((persisted.metadata or {}).get("inbound_processed")):
                return None
            failure_code = "message_submit_failed"
            try:
                result = runtime.orchestrator.submit(envelope)
            except SubmissionCommitUnknown:
                raise
            except Exception:
                released = not attachment_ids
                if attachment_ids and provider is not None:
                    try:
                        release_attachments(
                            provider,
                            attachment_ids,
                            message_id=client_message_id,
                        )
                        released = True
                    except Exception:
                        log.warning(
                            "Failed to release browser attachments after submit failure",
                            exc_info=True,
                        )
                if released and not runtime.store.delete_message(persisted.message_id):
                    log.error("Failed to roll back browser chat message %s", persisted.message_id)
                raise
            mark_chat_message_processed(drive_root, persisted.message_id)
            return result

    try:
        result = await asyncio.to_thread(_commit_message)
        if result is _MESSAGE_CONFLICT:
            await _send_ws_error(
                websocket,
                "duplicate client_message_id has different payload",
                code="message_conflict",
            )
            return
        if getattr(result, "intent", None) == TaskIntentKind.SLASH_COMMAND:
            await asyncio.to_thread(
                handle_slash_command,
                result.slash_command,
                chat_id=BROWSER_CHAT_ID,
                ctx=_command_ctx(websocket, runtime),
                source="browser",
            )
    except SubmissionCommitUnknown:
        log.warning("Browser task submission commit status is unknown", exc_info=True)
        await _send_ws_error(
            websocket,
            "task submission status is unknown; retry with the same client_message_id",
            code="message_submit_unknown",
            retryable=True,
        )
        return
    except Exception:
        log.warning("Failed to commit browser chat message", exc_info=True)
        persistence_failed = failure_code == "message_persistence_failed"
        await _send_ws_error(
            websocket,
            "failed to persist message" if persistence_failed else "failed to submit message",
            code=failure_code,
            retryable=True,
        )
        return

    if state is not None:
        try:
            # Re-read rather than write back the snapshot loaded before the commit: owner
            # state is one row, and the counters in it moved while this message was handled.
            await asyncio.to_thread(_touch_owner_activity, runtime.state_repo, ts)
        except Exception:
            log.warning("Failed to update browser chat activity timestamp", exc_info=True)

    await broadcast_ws(
        {
            "type": "chat",
            "role": "user",
            "content": content,
            "ts": ts,
            "source": "browser",
            "sender_session_id": sender_session_id,
            "client_message_id": client_message_id,
            "trace_id": trace_id,
            "attachments": public_attachments,
        }
    )


async def ws_endpoint(websocket: WebSocket) -> None:
    """Accept browser WebSocket connections and dispatch inbound chat frames."""
    await websocket.accept()
    await _ensure_background_tasks()
    total = _browser_hub.add_client(websocket)
    log.info("Browser WebSocket client connected (total: %d)", total)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await _send_ws_error(websocket, "invalid json", code="invalid_json")
                continue
            if not isinstance(msg, dict):
                await _send_ws_error(
                    websocket,
                    "invalid message payload",
                    code="invalid_message_payload",
                )
                continue

            msg_type = str(msg.get("type", "") or "")
            if msg_type == "chat":
                await _handle_inbound_chat(websocket, msg)
                continue
            if msg_type == "command":
                await _handle_inbound_command(websocket, msg)
                continue
            if msg_type:
                await _send_ws_error(
                    websocket,
                    f"unknown message type: {msg_type}",
                    code="unknown_message_type",
                )
                continue
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Browser WebSocket error: %s", exc)
    finally:
        total = _browser_hub.remove_client(websocket)
        log.info("Browser WebSocket client disconnected (total: %d)", total)


__all__ = [
    "BROWSER_CHAT_ID",
    "WS_MESSAGE_TYPES",
    "BrowserWebSocketHub",
    "attach_browser_ws_bridge",
    "broadcast_ws",
    "check_operator_command_policy",
    "get_browser_ws_hub",
    "reset_browser_ws_state_for_tests",
    "ws_endpoint",
]
