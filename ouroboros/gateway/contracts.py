"""Browser-facing HTTP and WebSocket payload contracts."""

from __future__ import annotations

import sys
from typing import Any, Literal, TypedDict

if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired


class TransportMetadata(TypedDict, total=False):
    kind: str
    conversation_id: str
    sender_label: str
    source_message_id: str
    target_uid: str


class AttachmentOutbound(TypedDict):
    id: str
    kind: Literal["image", "file"]
    filename: str
    mime: str
    size: int
    status: str
    expires_at: str
    url: NotRequired[str]


class ChatOutbound(TypedDict):
    type: Literal["chat"]
    role: Literal["user", "assistant", "system"]
    content: str
    ts: str
    markdown: NotRequired[bool]
    is_progress: NotRequired[bool]
    task_id: NotRequired[str]
    source: NotRequired[str]
    sender_label: NotRequired[str]
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    transport: NotRequired[TransportMetadata]
    telegram_chat_id: NotRequired[int]
    system_type: NotRequired[str]
    attachments: NotRequired[list[AttachmentOutbound]]


class ChatInbound(TypedDict):
    type: Literal["chat"]
    content: str
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    attachment_ids: NotRequired[list[str]]


class CommandInbound(TypedDict):
    type: Literal["command"]
    cmd: str
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]


class PhotoOutbound(TypedDict):
    type: Literal["photo"]
    role: Literal["user", "assistant"]
    image_base64: str
    mime: str
    caption: str
    ts: str
    source: NotRequired[str]
    filename: NotRequired[str]
    url: NotRequired[str]
    attachments: NotRequired[list[AttachmentOutbound]]


class TypingOutbound(TypedDict):
    type: Literal["typing"]
    action: str
    source: NotRequired[str]


class LogOutbound(TypedDict):
    type: Literal["log"]
    data: dict[str, Any]


class HealthResponse(TypedDict):
    status: Literal["ok"]


class BrowserHealthResponse(HealthResponse, total=False):
    version: NotRequired[str]
    runtime_version: NotRequired[str]
    app_version: NotRequired[str]


class ErrorResponse(TypedDict, total=False):
    error: str
    code: str
    message: str
    filename: str
    retryable: bool
    ok: bool


class StatusResponse(TypedDict):
    status: str


class HeartbeatOutbound(TypedDict):
    type: Literal["heartbeat"]
    ts: NotRequired[str]


class ExtensionLifecycleOutbound(TypedDict):
    type: Literal["extension_lifecycle"]
    skill: NotRequired[str]
    action: NotRequired[str]
    status: NotRequired[str]
    reason: NotRequired[str]
    data: NotRequired[dict[str, Any]]


class StateResponse(TypedDict):
    uptime: int
    workers_total: int
    workers_busy: int
    pending_count: int
    running_count: int
    branch: str
    sha: str
    spent_calls: int
    supervisor_ready: bool
    supervisor_error: str | None
    runtime_mode: str
    skills_repo_configured: bool
    github_token_configured: bool


class UploadResponse(TypedDict):
    ok: bool
    upload_id: str
    filename: str
    display_name: str
    path: str
    mime: str
    size: int
    attachment_id: str
    status: str
    expires_at: str
    url: str


class ChatHistoryResponse(TypedDict, total=False):
    messages: list[dict[str, Any]]
    has_more: bool
    next_before_ts: str
    error: str


HTTP_ENDPOINTS: tuple[str, ...] = (
    "GET /api/health",
    "GET /health/readiness",
    "GET /health/liveness",
)

BROWSER_HTTP_ENDPOINTS: tuple[str, ...] = (
    "GET /api/health",
    "GET /api/state",
    "GET /api/chat/history",
    "GET /api/chat/files/{path:path}",
    "POST /api/chat/upload",
    "DELETE /api/chat/upload",
    "WS /ws",
)

WS_MESSAGE_TYPES: tuple[str, ...] = (
    "chat",
    "command",
    "photo",
    "typing",
    "log",
    "heartbeat",
    "extension_lifecycle",
)

EXCLUDED_BROWSER_HTTP_ENDPOINTS: frozenset[str] = frozenset(
    {
        "GET /api/settings",
        "POST /api/settings",
        "POST /api/command",
        "POST /api/reset",
    }
)


__all__ = [
    "BROWSER_HTTP_ENDPOINTS",
    "EXCLUDED_BROWSER_HTTP_ENDPOINTS",
    "HTTP_ENDPOINTS",
    "WS_MESSAGE_TYPES",
    "AttachmentOutbound",
    "BrowserHealthResponse",
    "ChatHistoryResponse",
    "ChatInbound",
    "ChatOutbound",
    "CommandInbound",
    "ErrorResponse",
    "ExtensionLifecycleOutbound",
    "HealthResponse",
    "HeartbeatOutbound",
    "LogOutbound",
    "PhotoOutbound",
    "StateResponse",
    "StatusResponse",
    "TransportMetadata",
    "TypingOutbound",
    "UploadResponse",
]
