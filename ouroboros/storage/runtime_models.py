"""Pydantic models for runtime task, message, usage, and audit metadata."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

TaskStatus = Literal[
    "requested",
    "scheduled",
    "queued",
    "running",
    "waiting_confirmation",
    "completed",
    "rejected_duplicate",
    "failed",
    "interrupted",
    "cancelled",
    "budget_exceeded",
]

TaskSource = Literal["dialogs", "sberchat", "a2a", "web", "browser", "internal", "system", "unknown"]
ActorType = Literal["owner", "service", "agent", "system"]
TASK_SOURCES = frozenset({"dialogs", "sberchat", "a2a", "web", "browser", "internal", "system", "unknown"})


def normalize_task_source(value: Any) -> TaskSource:
    text = str(value or "unknown").strip().lower()
    if text.startswith("skill:"):
        return "internal"
    if text == "ingress":
        return "dialogs"
    return cast(TaskSource, text if text in TASK_SOURCES else "unknown")


def is_dialogs_source(value: Any) -> bool:
    """Whether a source is served by the Dialogs transport, `sberchat` label included."""
    return normalize_task_source(value) in {"dialogs", "sberchat"}


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


class RuntimeModel(BaseModel):
    """Common runtime model settings."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OwnerRuntimeState(RuntimeModel):
    """Typed owner/session runtime snapshot (DB row per tenant)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    owner_id: int | str | None = None
    owner_chat_id: int | str | None = None
    owner_chat_source: str | None = None
    spent_calls: int = 0
    spent_tokens_prompt: int = 0
    spent_tokens_completion: int = 0
    spent_tokens_cached: int = 0
    session_id: str = ""
    last_owner_message_at: str = ""
    provider_state: dict[str, Any] = Field(default_factory=dict)
    # Declared, not an extra key: ensure_state_defaults drops what the model does not know.
    context_consolidation_failures: dict[str, Any] = Field(default_factory=dict)


class RuntimeSession(RuntimeModel):
    session_id: str
    source: TaskSource = "unknown"
    owner_external_id: str = ""
    auth_context_hash: str = ""
    status: Literal["active", "expired", "revoked"] = "active"
    created_at: str = ""
    last_seen_at: str = ""
    expires_at: str = ""

    @field_validator("session_id")
    @classmethod
    def _session_id_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("session_id is required")
        return text


class ContextSummary(RuntimeModel):
    summary_id: str
    conversation_id: str
    source_offsets_ref: str = ""
    summary_ref: str = ""
    token_budget: int = 0
    summary_hash: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary_id", "conversation_id")
    @classmethod
    def _summary_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("summary_id and conversation_id are required")
        return text


class ConversationMessage(RuntimeModel):
    message_id: str
    conversation_id: str
    session_id: str = ""
    source: TaskSource = "unknown"
    source_message_id: str = ""
    actor_type: ActorType = "owner"
    channel: str = ""
    content: str = ""
    content_ref: str = ""
    content_hash: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message_id", "conversation_id")
    @classmethod
    def _message_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("message_id and conversation_id are required")
        return text

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self.metadata or {})

    @property
    def chat_id(self) -> Any:
        return self.meta.get("chat_id")

    @property
    def message_type(self) -> str:
        return str(self.meta.get("type") or "")

    @property
    def is_progress(self) -> bool:
        return bool(self.meta.get("is_progress"))

    @property
    def watermark_id(self) -> str:
        return str(self.message_id or "")

    def display_text(self) -> str:
        if self.is_progress:
            return str(self.meta.get("content") or self.content or "")
        return str(self.content or "")


class TaskRequest(RuntimeModel):
    id: str = ""
    type: str = "task"
    chat_id: int | str | None = None
    text: str = ""
    description: str = ""
    context: str = ""
    depth: int = 0
    parent_task_id: str = ""
    source: TaskSource = "unknown"
    conversation_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    priority: int = 1
    attempt: int = Field(default=1, alias="_attempt")
    queued_at: str = ""
    task_constraint: dict[str, Any] = Field(default_factory=dict)
    image_base64: str = ""
    image_mime: str = ""
    image_caption: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    transport: dict[str, Any] = Field(default_factory=dict)
    is_direct_chat: bool = Field(default=False, alias="_is_direct_chat")

    @field_validator("depth", "priority", "attempt", mode="before")
    @classmethod
    def _coerce_int(cls, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @field_validator("id", mode="before")
    @classmethod
    def _strip_id(cls, value: Any) -> str:
        return _clean_str(value)

    def to_task_dict(self) -> dict[str, Any]:
        """Return the legacy task dict consumed by the agent."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not data.get("_attempt"):
            data["_attempt"] = 1
        if data.get("_is_direct_chat") is False:
            data.pop("_is_direct_chat", None)
        for key in ("attempt", "is_direct_chat"):
            data.pop(key, None)
        return data


class TaskRecord(RuntimeModel):
    task_id: str
    session_id: str = ""
    conversation_id: str = ""
    source: TaskSource = "unknown"
    correlation_id: str = ""
    status: TaskStatus = "queued"
    task_type: str = "task"
    chat_id: int | str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    input_ref: str = ""
    result: str = ""
    result_ref: str = ""
    error_code: str = ""
    priority: int = 1
    attempt: int = 1
    worker_id: int | None = None
    queued_at: str = ""
    created_at: str = ""
    started_at: str = ""
    heartbeat_at: str = ""
    finished_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id")
    @classmethod
    def _task_id_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("task_id is required")
        return text

    def to_agent_input(self) -> dict[str, Any]:
        """Legacy task dict consumed by OuroborosAgent.handle_task."""
        if self.input:
            data = dict(self.input)
            data.setdefault("id", self.task_id)
            data.setdefault("type", self.task_type)
            data.setdefault("chat_id", self.chat_id)
            return data
        return TaskRequest(
            id=self.task_id,
            type=self.task_type,
            chat_id=self.chat_id,
            source=self.source,
            conversation_id=self.conversation_id,
            correlation_id=self.correlation_id,
        ).to_task_dict()


class ToolInvocation(RuntimeModel):
    tool_invocation_id: str = ""
    task_id: str = ""
    conversation_id: str = ""
    tool_name: str = ""
    status: str = ""
    args_redacted: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    result_index: dict[str, Any] = Field(default_factory=dict)
    search_terms: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = ""
    sensitivity: str = "normal"
    retention_class: str = "summary"
    created_at: str = ""
    expires_at: str = ""


class TaskEpisode(RuntimeModel):
    episode_id: str = ""
    task_id: str = ""
    conversation_id: str = ""
    created_at: str = ""
    user_text: str = ""
    answer_preview: str = ""
    tools_used: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, Any] = Field(default_factory=dict)
    search_terms: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: str = ""


class LlmUsageRecord(RuntimeModel):
    usage_id: str = ""
    task_id: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(RuntimeModel):
    event_id: str = ""
    timestamp: str = ""
    correlation_id: str = ""
    actor_type: str = ""
    resource_type: str = ""
    resource_id: str = ""
    event_type: str
    log_stream: str = "events"
    decision: str = ""
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _event_type_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("event_type is required")
        return text


class SupervisorEvent(RuntimeModel):
    """Validated supervisor event envelope.

    Event payloads stay flexible during migration, but every event now has a
    typed envelope and a non-empty discriminant.
    """

    type: str
    ts: str = ""
    task_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _type_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("event.type is required")
        return text

    @classmethod
    def from_event_dict(cls, event: dict[str, Any]) -> SupervisorEvent:
        event_type = str(event.get("type") or "").strip()
        payload = {key: value for key, value in event.items() if key not in {"type", "ts", "task_id"}}
        return cls(
            type=event_type,
            ts=str(event.get("ts") or ""),
            task_id=str(event.get("task_id") or ""),
            payload=payload,
            **payload,
        )
