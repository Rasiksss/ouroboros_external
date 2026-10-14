"""Browser-facing HTTP and WebSocket payload contracts."""

from __future__ import annotations

import sys
from typing import Literal, TypedDict
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue, computed_field

from ouroboros.contracts.attachments import AttachmentMetadata, FileStatus, PublicAttachment, public_attachment
from ouroboros.storage.task_results import JobDetail, JobSummary

if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired


class BrowserAttachment(PublicAttachment):
    url: NotRequired[str]


def browser_attachment(metadata: AttachmentMetadata) -> BrowserAttachment:
    result: BrowserAttachment = {**public_attachment(metadata)}
    if result.get("status") in {"draft", "ready"}:
        result["url"] = f"api/chat/files/{metadata['id']}/{quote(metadata['filename'], safe='')}"
    return result


class TransportMetadata(TypedDict, total=False):
    kind: str
    sender_label: str
    source_message_id: str
    target_uid: str


class ChatOutbound(TypedDict):
    type: Literal["chat"]
    role: Literal["user", "assistant", "system"]
    content: str
    ts: str
    markdown: NotRequired[bool]
    is_progress: NotRequired[bool]
    task_id: NotRequired[str]
    source: NotRequired[str]
    trace_id: NotRequired[str]
    sender_label: NotRequired[str]
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    transport: NotRequired[dict[str, JsonValue]]
    telegram_chat_id: NotRequired[int]
    system_type: NotRequired[str]
    attachments: NotRequired[list[BrowserAttachment]]


class ChatInbound(TypedDict):
    type: Literal["chat"]
    content: str
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    attachment_ids: NotRequired[list[str]]
    detached: NotRequired[bool]


class JobsResponse(TypedDict):
    detached_enabled: bool
    jobs: list[JobSummary]
    truncated: bool


class JobResponse(TypedDict):
    job: JobDetail


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
    attachments: NotRequired[list[BrowserAttachment]]


class TypingOutbound(TypedDict):
    type: Literal["typing"]
    action: str
    source: NotRequired[str]


class LogOutbound(TypedDict):
    type: Literal["log"]
    data: dict[str, JsonValue]


class HealthResponse(TypedDict):
    status: Literal["ok"]


class BrowserHealthResponse(HealthResponse, total=False):
    version: NotRequired[str]
    runtime_version: NotRequired[str]
    app_version: NotRequired[str]


#: The codes the browser transport answers its own refusals with. Free-form on the
#: wire, named here because a client tells them apart: ``COMMAND_NOT_ALLOWED`` is a
#: verdict about the command, ``COMMAND_CHECK_UNAVAILABLE`` is this instance being
#: unable to judge it at all — one is fixed by editing the command, the other by
#: configuring the deployment.
COMMAND_NOT_ALLOWED = "command_not_allowed"
COMMAND_CHECK_UNAVAILABLE = "command_check_unavailable"
COMMAND_REFUSAL_CODES = (COMMAND_NOT_ALLOWED, COMMAND_CHECK_UNAVAILABLE)


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
    data: NotRequired[dict[str, JsonValue]]


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


class ConfirmationDecisionResponse(TypedDict):
    ok: bool
    status: str


class ConfirmationRequestOutbound(TypedDict):
    """A gated call put to the owner in fields; ``arguments_preview`` is redacted and truncated."""

    type: Literal["confirmation_request"]
    confirmation_id: str
    task_id: str
    tool: str
    arguments_preview: str
    arguments_truncated: bool
    text: str
    ts: str
    source: NotRequired[str]


BrowserOutbound = (
    ChatOutbound
    | PhotoOutbound
    | TypingOutbound
    | LogOutbound
    | HeartbeatOutbound
    | ExtensionLifecycleOutbound
    | ConfirmationRequestOutbound
)


class HistoryConfirmation(TypedDict):
    confirmation_id: str
    task_id: str
    tool: str
    arguments_preview: str
    arguments_truncated: bool
    open: bool


class ChatHistoryRecord(TypedDict, total=False):
    message_id: str
    chat_id: str
    text: str
    role: Literal["user", "assistant", "system"]
    ts: str
    is_progress: bool
    markdown: bool
    source: str
    sender_label: str
    sender_session_id: str
    client_message_id: str
    task_id: str
    system_type: str
    attachments: list[BrowserAttachment]
    confirmation: HistoryConfirmation


class BrowserEcho(TypedDict, total=False):
    sender_session_id: str
    client_message_id: str
    attachments: list[BrowserAttachment]


class ChatHistoryResponse(TypedDict, total=False):
    messages: list[ChatHistoryRecord]
    has_more: bool
    next_before_ts: str
    error: str


HTTP_ENDPOINTS: tuple[str, ...] = (
    "GET /api/health",
    "GET /start",
    "GET /ready",
    "GET /live",
)

BROWSER_HTTP_ENDPOINTS: tuple[str, ...] = (
    "GET /api/chat/schedules",
    "DELETE /api/chat/schedules/{schedule_id}",
    "GET /api/health",
    "GET /api/state",
    "GET /api/chat/history",
    "GET /api/chat/files/{path:path}",
    "POST /api/chat/upload",
    "POST /api/files",
    "GET /api/files/entries",
    "GET /api/files/{id}",
    "GET /api/files/{id}/content",
    "GET /api/files/{id}/messages",
    "PATCH /api/files/{id}",
    "DELETE /api/files/{id}",
    "POST /api/folders",
    "PATCH /api/folders/{id}",
    "DELETE /api/folders/{id}",
    "DELETE /api/chat/upload",
    "POST /api/confirmations/{confirmation_id}",
    "GET /api/jobs",
    "GET /api/jobs/{job_id}",
    "POST /api/jobs/{job_id}/stop",
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
    "confirmation_request",
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
    "BrowserHealthResponse",
    "ChatHistoryResponse",
    "ChatInbound",
    "ChatOutbound",
    "CommandInbound",
    "ConfirmationDecisionResponse",
    "ConfirmationRequestOutbound",
    "ErrorResponse",
    "ExtensionLifecycleOutbound",
    "HealthResponse",
    "HeartbeatOutbound",
    "HistoryConfirmation",
    "JobDetail",
    "JobResponse",
    "JobSummary",
    "JobsResponse",
    "LogOutbound",
    "PhotoOutbound",
    "StateResponse",
    "StatusResponse",
    "TransportMetadata",
    "TypingOutbound",
    "UploadResponse",
]


class FileInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    folder_id: str | None
    mime: str
    size: int
    status: FileStatus
    created_at: str
    updated_at: str
    expires_at: str | None

    @computed_field
    def url(self) -> str:
        return f"/api/files/{self.id}/content"
