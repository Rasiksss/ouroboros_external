"""Pydantic models for runtime task, message, usage, and audit metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.attachments import (
    ATTACHMENTS_ADAPTER,
    TERMINAL_ATTACHMENTS_ADAPTER,
    AttachmentMetadata,
    TerminalAttachment,
)
from ouroboros.contracts.tool_confirmation import CONFIRMATION_PENDING
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)
_TERMINAL_DELIVERY_ADAPTER = TypeAdapter(dict[str, str])

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

#: The statuses of an attempt that is still going to do something — read this wherever "is this
#: task still in flight" is asked. A copy written out as literals disagrees the moment one is added.
ACTIVE_STATUSES: tuple[TaskStatus, ...] = ("queued", "scheduled", "interrupted", "running", "waiting_confirmation")

#: Where a parked run left its task workspace: on the record, because the resumed run needs it
#: before it builds anything that could read a snapshot.
PARKED_WORKSPACE_KEY = "parked_workspace"

TaskSource = Literal["dialogs", "sberchat", "a2a", "web", "browser", "internal", "system", "unknown"]
ActorType = Literal["owner", "service", "agent", "system"]
TASK_SOURCES = frozenset({"dialogs", "sberchat", "a2a", "web", "browser", "internal", "system", "unknown"})


def normalize_task_source(value: Any) -> TaskSource:
    text = str(value or "unknown").strip().lower()
    if text.startswith("skill:") or text == "skill-bridge":
        return "internal"
    if text == "ingress":
        return "dialogs"
    return cast(TaskSource, text if text in TASK_SOURCES else "unknown")


def source_aliases(surface: SurfaceId) -> tuple[str, ...]:
    return tuple(sorted(source for source in TASK_SOURCES if SurfaceId.coerce(source) == surface))


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

    #: The transport user id of whoever claimed this instance. Not ``OwnerScope.owner_id``,
    #: which holds the configured owner: two different ideas that share a name.
    owner_id: int | str | None = None
    #: The owner's chat, whole, written where ownership is claimed. ``None`` means
    #: unclaimed — a row written before this field existed has no scope, and none is
    #: synthesised for it.
    owner_scope: dict[str, str] | None = None
    spent_calls: int = 0
    spent_tokens_prompt: int = 0
    spent_tokens_completion: int = 0
    spent_tokens_cached: int = 0
    session_id: str = ""
    last_owner_message_at: str = ""
    provider_state: dict[str, Any] = Field(default_factory=dict)

    @property
    def scope(self) -> OwnerScope | None:
        """The owner's stored scope, or ``None`` while ownership is unclaimed."""
        return OwnerScope.from_dict(self.owner_scope) if self.owner_scope else None

    @property
    def owner_chat_id(self) -> str | None:
        """The owner's chat, derived from the stored scope rather than held beside it.

        The stored axis verbatim. Converting here would be a second copy of the seam's
        rule in a layer that may not import the seam, and the two spellings would drift:
        this property would read back ``1`` for the two distinct conversations ``"1"`` and
        ``"01"``, so one would authorize runtime commands for the other. Every reader
        compares as a string already.
        """
        scope = self.scope
        return scope.chat_id if scope is not None else None

    @property
    def owner_chat_source(self) -> str | None:
        """The owner's chat source, derived. Never a sentinel: absent means unclaimed."""
        scope = self.scope
        return str(scope.surface_id) if scope is not None else None


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
    summary_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    owner_id: str
    agent_id: str
    chat_id: str
    source_offsets_ref: str = ""
    summary_ref: str = ""
    token_budget: int = 0
    summary_hash: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary_id", "owner_id", "agent_id")
    @classmethod
    def _summary_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("summary_id, owner_id and agent_id are required")
        return text


class ConsolidationLease(RuntimeModel):
    """One ownership pair's consolidation lease, and what its failures have earned.

    Both facts share a row because both answer one decision at one instant: whether this
    trigger may start a run. They are also written together — a release that landed while
    the failure increment did not would hand the pair on with the gate wide open.

    ``lease_expires_at`` and ``last_failure_at`` are wall-clock ISO rather than a monotonic
    reading: the row is read by instances that never shared a clock reading.
    """

    owner_id: str
    agent_id: str
    #: Empty means unleased. Never a worker lane: it names the process, so a renewal or a
    #: release from a run a peer displaced matches no row.
    holder_id: str = ""
    lease_expires_at: str = ""
    consecutive_failures: int = 0
    last_failure_at: str = ""
    updated_at: str = ""

    @field_validator("owner_id", "agent_id")
    @classmethod
    def _lease_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("owner_id and agent_id are required")
        return text

    def is_held(self, now_iso: str) -> bool:
        """Whether a live holder has this pair — the predicate the acquire writes as SQL."""
        return bool(self.holder_id) and self.lease_expires_at > str(now_iso)


#: What a release tells the store about the run that ended, in the capability's own words:
#: a completed run resets the throttle, a failed one advances it, and everything else —
#: a stale cursor, a lost lease, a stop, an empty backlog — is neither and leaves it alone.
ConsolidationOutcome = Literal["completed", "failed", "neither"]


class DeploymentKeyspace(RuntimeModel):
    """Where this deployment's object keys live: backend, bucket, and key prefix.

    One row per runtime database, because the runtime database is the deployment
    boundary. Recorded by the first instance to start under the multi-user mode and
    compared by every later one — a per-pod tenant id or prefix is a private keyspace
    inside a correctly shared bucket, and no process can read its peers' environment.
    """

    backend: str
    bucket: str = ""
    endpoint: str = ""
    key_prefix: str
    recorded_at: str = ""

    @field_validator("backend", "key_prefix")
    @classmethod
    def _keyspace_parts_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("backend and key_prefix are required")
        return text

    def differs_from(self, other: DeploymentKeyspace) -> bool:
        """Whether these two instances would write to different places."""
        return (self.backend, self.bucket, self.endpoint, self.key_prefix) != (
            other.backend,
            other.bucket,
            other.endpoint,
            other.key_prefix,
        )

    def describe(self) -> str:
        return (
            f"backend={self.backend} bucket={self.bucket or '-'} "
            f"endpoint={self.endpoint or '-'} prefix={self.key_prefix}"
        )


class ConversationMessage(RuntimeModel):
    message_id: str
    #: The stored identity: the ownership folded over what the message was identified by
    #: before the axes existed. The primary key is a surrogate, so that a row can be
    #: re-scoped without rewriting an encrypted payload that carries its own id.
    dedup_key: str
    owner_id: str
    agent_id: str
    chat_id: str
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

    @field_validator("message_id", "dedup_key", "owner_id", "agent_id")
    @classmethod
    def _message_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("message_id, dedup_key, owner_id and agent_id are required")
        return text

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self.metadata or {})

    @property
    def message_type(self) -> str:
        return str(self.meta.get("type") or "")

    @property
    def is_progress(self) -> bool:
        return bool(self.meta.get("is_progress"))

    @property
    def watermark_id(self) -> str:
        return str(self.message_id or "")

    @property
    def attachments(self) -> list[AttachmentMetadata]:
        return ATTACHMENTS_ADAPTER.validate_python(self.metadata.get("attachments") or [])

    def display_text(self) -> str:
        if self.is_progress:
            return str(self.meta.get("content") or self.content or "")
        return str(self.content or "")


class TaskRequest(RuntimeModel):
    id: str = ""
    job_id: str = ""
    detached: bool = False
    inherited_context_seed: str = ""
    committed_summary_snapshot: str = ""
    through_message_id: str = ""
    context_captured: bool = False
    history_message_ids: list[str] | None = None
    task_snapshot: list[TaskRecord] | None = None
    episode_snapshot: dict[str, str] | None = None
    recent_task_ids: list[str] = Field(default_factory=list)
    type: str = "task"
    chat_id: int | str | None = None
    text: str = ""
    description: str = ""
    context: str = ""
    depth: int = 0
    parent_task_id: str = ""
    source: TaskSource = "unknown"
    #: The minted ``OwnerScope``, serialized. The one identity the agent reads, and where
    #: the stored row's ownership columns are filled from.
    scope: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = ""
    trace_id: str = ""
    priority: int = 1
    attempt: int = Field(default=1, alias="_attempt")
    queued_at: str = ""
    image_base64: str = ""
    image_mime: str = ""
    image_caption: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    attachments: list[AttachmentMetadata] = Field(default_factory=list)
    transport: dict[str, Any] = Field(default_factory=dict)

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

    @model_validator(mode="after")
    def _default_job_id(self) -> TaskRequest:
        self.job_id = self.job_id or self.id
        return self

    def to_task_dict(self) -> dict[str, Any]:
        """The task dict consumed by the agent: identity is ``scope`` and nothing else."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        if not data.get("_attempt"):
            data["_attempt"] = 1
        data.pop("attempt", None)
        # Not identity: the agent addresses through ``scope``. ``extra="allow"`` means one
        # can ride in on a validated payload, so they are dropped rather than never set.
        data.pop("conversation_id", None)
        data.pop("session_id", None)
        return data


class TaskRecord(RuntimeModel):
    surface_id: str = ""
    task_id: str
    job_id: str = ""
    lineage_ordinal: int = 0
    detached: bool = False
    owner_id: str
    agent_id: str
    session_id: str = ""
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
    #: The run that planned this one, empty for a root. A column rather than a payload key
    #: because the claim's one-run-per-owner rule counts roots, and it decides that inside
    #: its own statement.
    parent_task_id: str = ""
    #: Which instance holds this run. ``worker_id`` beside it stays a lane number within
    #: that instance: every instance has a lane 0, so a lane alone is not an address.
    runtime_id: str | None = None
    #: When a stop was first asked for, or ``None``. The intent is durable, so a stop
    #: reaches a run held by another instance.
    stop_requested_at: str | None = None
    #: When the soft-timeout notice was claimed, or ``None``. Durable and claimed by a
    #: statement rather than flagged, so a second instance's sweep does not announce it again.
    soft_notice_at: str | None = None
    terminal_published_at: str | None = None
    terminal_publish_claim: str | None = None
    terminal_publish_after: str | None = None
    queued_at: str = ""
    created_at: str = ""
    started_at: str = ""
    heartbeat_at: str = ""
    finished_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id", "owner_id", "agent_id")
    @classmethod
    def _task_keys_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("task_id, owner_id and agent_id are required")
        return text

    @model_validator(mode="after")
    def _default_job_id(self) -> TaskRecord:
        self.job_id = self.job_id or self.task_id
        return self

    def stored_scope(self) -> dict[str, str] | None:
        """Reconstruct the persisted identity from canonical row columns."""
        chat = str(self.chat_id if self.chat_id is not None else "").strip()
        surface = SurfaceId.coerce(self.surface_id or self.source)
        if not chat or surface is None:
            return None
        return {
            "owner_id": self.owner_id,
            "agent_id": self.agent_id,
            "surface_id": str(surface),
            "chat_id": chat,
        }

    @property
    def terminal_delivery(self) -> dict[str, str]:
        return _TERMINAL_DELIVERY_ADAPTER.validate_python(self.metadata.get("terminal_delivery", {}))

    @property
    def terminal_attachments(self) -> list[TerminalAttachment]:
        return TERMINAL_ATTACHMENTS_ADAPTER.validate_python(self.metadata.get("terminal_attachments") or [])

    def to_agent_input(self) -> dict[str, Any]:
        """Legacy task dict consumed by OuroborosAgent.handle_task."""
        if self.input:
            data = dict(self.input)
            data.setdefault("id", self.task_id)
            data.setdefault("job_id", self.job_id)
            data.setdefault("detached", self.detached)
            data.setdefault("type", self.task_type)
            data.setdefault("chat_id", self.chat_id)
        else:
            data = TaskRequest(
                id=self.task_id,
                type=self.task_type,
                chat_id=self.chat_id,
                source=self.source,
                correlation_id=self.correlation_id,
            ).to_task_dict()
        if not data.get("scope"):
            # An empty ``scope`` reads as an identity that is present and says nothing, and
            # every consumer refuses it. Either the row's columns still name one, or the key
            # is absent and the caller can see that it is.
            data.pop("scope", None)
            recovered = self.stored_scope()
            if recovered is not None:
                data["scope"] = recovered
        data["execution_claim"] = str(self.metadata.get("execution_claim") or "")
        parked_workspace = str(self.metadata.get(PARKED_WORKSPACE_KEY) or "")
        if parked_workspace:
            data[PARKED_WORKSPACE_KEY] = parked_workspace
        return data


def same_task_request(record: TaskRecord, request: TaskRequest) -> bool:
    """Whether an idempotent root submission matches its stored immutable input."""
    return (
        str(record.input.get("text") or "") == request.text
        and list(record.input.get("attachment_ids") or []) == request.attachment_ids
        and record.detached == request.detached
    )


class TaskStopState(RuntimeModel):
    """What a stop check reads off a record, both facts in one round trip.

    A terminal record stops its run as surely as a recorded stop does: whatever closed it
    already decided the run is over.
    """

    stop_requested: bool
    is_terminal: bool


class ScopedRow(RuntimeModel):
    """A row filed under all four axes of the scope its work ran under."""

    owner_id: str
    agent_id: str
    surface_id: str
    chat_id: str

    @field_validator("owner_id", "agent_id", "surface_id", "chat_id")
    @classmethod
    def _axes_required(cls, value: Any) -> str:
        text = _clean_str(value)
        if not text:
            raise ValueError("owner_id, agent_id, surface_id and chat_id are required")
        return text


class ToolInvocation(ScopedRow):
    tool_invocation_id: str = ""
    task_id: str = ""
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


class ToolConfirmation(ScopedRow):
    """One owner decision a parked run is waiting on, or the decision it was resumed with."""

    confirmation_id: str = ""
    task_id: str = ""
    tool_name: str = ""
    call_ordinal: int = 0
    round_idx: int = 0
    args_hash: str = ""
    status: str = CONFIRMATION_PENDING
    created_at: str = ""
    decided_at: str = ""


class TaskEpisode(ScopedRow):
    episode_id: str = ""
    task_id: str = ""
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

    def snapshot_version(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


class LlmUsageRecord(RuntimeModel):
    usage_id: str = ""
    #: Whose spend this was, written when it is recorded. Never derived later by joining
    #: through ``task_id``: spend outside a task has no task to join through, and a deleted
    #: task would take its rows' attribution with it.
    owner_id: str = ""
    agent_id: str = ""
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
    #: Whose action this records. Same rule as spend: written at the time, not derived.
    owner_id: str = ""
    agent_id: str = ""
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

    @classmethod
    def runtime_event(
        cls,
        stream: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        owner_id: str,
        agent_id: str,
        resource_type: str = "task",
        resource_id: str = "",
        decision: str = "",
        reason: str = "",
    ) -> AuditEvent:
        """One runtime event's audit row, whether journaled alone or written inside the change it records."""
        metadata = {
            "ts": str(payload.get("ts") or utc_now_iso()),
            "type": str(event_type or payload.get("type") or "event"),
            "log_stream": stream,
            "log_kind": "runtime_event",
            **payload,
        }
        return cls(
            owner_id=owner_id,
            agent_id=agent_id,
            timestamp=str(metadata.get("ts") or utc_now_iso()),
            event_type=str(metadata.get("type") or "event"),
            log_stream=stream,
            resource_type=resource_type,
            resource_id=str(resource_id or metadata.get("task_id") or ""),
            decision=decision,
            reason=reason,
            metadata=metadata,
        )


def stored_task_scope(payload: Any) -> OwnerScope | None:
    """The scope on a stored task's input, or ``None`` when the row carries none.

    Lives beside the record rather than inside either component that reads one: the runtime
    reads it to build a run's stop signal, the queue reads it to address a notice.
    """
    if not payload:
        return None
    try:
        return OwnerScope.from_dict(payload)
    except ValueError:
        log.debug("Stored task carries an unusable scope", exc_info=True)
        return None
