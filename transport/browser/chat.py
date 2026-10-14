from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import unquote, urlparse

import anyio
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.contracts.owner_scope import CHAT_HEADER_NAME
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.chat_store import history_cursor
from ouroboros.storage.file_catalog import FileCatalog, FileError, FileFailure
from ouroboros.storage.message_history import HistoryWindow
from ouroboros.storage.runtime_models import ConversationMessage
from transport.browser.contracts import ChatHistoryRecord, ChatHistoryResponse, browser_attachment
from transport.browser.files import FileContent, file_errors, upload
from transport.browser.identity import browser_identity

log = logging.getLogger(__name__)


def _actor_role(actor_type: str) -> Literal["user", "assistant", "system"] | None:
    if actor_type == "owner":
        return "user"
    if actor_type == "agent":
        return "assistant"
    if actor_type == "system":
        return "system"
    if actor_type == "service":
        return "assistant"
    return None


def _message_to_history_record(message: ConversationMessage, open_confirmation_id: str | None) -> ChatHistoryRecord:
    role = _actor_role(str(message.actor_type or ""))
    if role is None:
        return {}
    metadata = dict(message.metadata or {})
    is_progress = bool(metadata.get("is_progress"))
    record: ChatHistoryRecord = {
        "message_id": message.message_id,
        "chat_id": message.chat_id,
        "text": message.display_text(),
        "role": role,
        "ts": str(message.created_at or ""),
        "is_progress": is_progress,
        "markdown": str(metadata.get("format") or "").lower() == "markdown",
        "source": str(message.source or metadata.get("source") or ""),
        "sender_label": str(metadata.get("sender_label") or ""),
        "sender_session_id": str(metadata.get("sender_session_id") or ""),
        "client_message_id": str(message.source_message_id or message.message_id or ""),
        "task_id": str(metadata.get("task_id") or ""),
    }
    transport = metadata.get("transport")
    if role == "assistant" and record["source"] == "browser" and isinstance(transport, dict):
        record["client_message_id"] = str(transport.get("source_message_id") or record["client_message_id"])
    asked = transport.get(CONFIRMATION_TRANSPORT_KEY) if isinstance(transport, dict) else None
    if role == "assistant" and isinstance(asked, dict):
        confirmation_id = str(asked.get("confirmation_id") or "")
        record["confirmation"] = {
            "confirmation_id": confirmation_id,
            "task_id": str(asked.get("task_id") or ""),
            "tool": str(asked.get("tool") or ""),
            "arguments_preview": str(asked.get("arguments_preview") or ""),
            "arguments_truncated": bool(asked.get("arguments_truncated")),
            "open": confirmation_id == open_confirmation_id,
        }
    if metadata.get("type"):
        record["system_type"] = str(metadata["type"])
    attachments = [browser_attachment(item) for item in message.attachments]
    if attachments:
        record["attachments"] = attachments
    return record


@file_errors
async def api_chat_history(request: Request) -> Response:
    headers = dict(request.headers)
    if chat_id := request.query_params.get("chat_id"):
        headers[CHAT_HEADER_NAME.lower()] = chat_id
    scope = await browser_identity.resolver.scope_for(headers)
    window = await request.app.state.owner_history_repository.load_history(
        scope=scope,
        limit=int(request.query_params.get("limit", "1000")),
        before_ts=request.query_params.get("before_ts", ""),
    )
    return await anyio.to_thread.run_sync(_history_response, window)


def _history_response(window: HistoryWindow) -> JSONResponse:
    messages = window.messages()
    payload: ChatHistoryResponse = {
        "messages": [_message_to_history_record(message, window.open_confirmation_id) for message in messages]
    }
    if window.more:
        payload["has_more"] = True
        payload["next_before_ts"] = history_cursor(messages[0])
    return JSONResponse(payload)


@file_errors
async def api_chat_upload(request: Request) -> Response:
    return await upload(request, draft=True)


def _file_id(raw: str) -> str:
    value = raw.strip().strip("'\"")
    parsed = urlparse(value)
    if parsed.scheme and parsed.path:
        value = parsed.path
    path = unquote(value).strip().lstrip("/").removeprefix("api/chat/files/")
    parts = [part for part in path.split("/") if part]
    if not parts or ".." in parts or "_meta" in parts:
        raise FileError("invalid file path", FileFailure.INVALID_INPUT)
    if parts[0] == "uploads":
        parts = parts[1:]
    if parts and parts[0] == "chat":
        parts = parts[1:]
    if not parts:
        raise FileError("missing file id", FileFailure.INVALID_INPUT)
    return parts[0]


@file_errors
async def api_chat_file_download(request: Request) -> Response:
    scope = await browser_identity.resolver.scope_for(request.headers)
    file_id = _file_id(request.path_params["path"])
    async with file_transaction() as conn:
        file = await conn.run_sync(lambda c: FileCatalog(c, scope).file(file_id))
    return FileContent(
        request.app.state.file_blob, scope, file, request.query_params.get("download") in {"1", "true", "yes"}
    )


@file_errors
async def api_chat_upload_delete(request: Request) -> Response:
    scope = await browser_identity.resolver.scope_for(request.headers)
    file_id = request.query_params.get("id", "")
    if not file_id and request.headers.get("content-type", "").startswith("application/json"):
        body = DraftCancel.model_validate(await request.json())
        file_id = _file_id(body.path or body.filename)
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise FileError("invalid upload id", FileFailure.INVALID_INPUT)
    async with file_transaction() as conn:
        await conn.run_sync(lambda c: FileCatalog(c, scope).delete_file(file_id, draft_only=True))
    return JSONResponse({"status": "ok"})


class DraftCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = ""
    filename: str = ""
