"""Runtime metadata store protocol (SQLite file or PostgreSQL)."""

from __future__ import annotations

import pathlib
from collections.abc import Iterable
from typing import Any, Protocol

from sqlalchemy.engine import Engine

from ouroboros.storage.runtime_models import (
    AuditEvent,
    ContextSummary,
    ConversationMessage,
    LlmUsageRecord,
    RuntimeSession,
    TaskEpisode,
    TaskRecord,
    TaskRequest,
    TaskStatus,
    ToolInvocation,
)


class MessageAnchorMissingError(LookupError):  # A missing cursor differs from an empty visible tail.
    pass


class RuntimeStore(Protocol):
    """Transactional task/session/message metadata repository."""

    @property
    def engine(self) -> Engine:
        """The SQL engine behind this store.

        Declared because the core-memory adapters run their own statements on
        it: an undeclared dependency reached through ``getattr`` is one mypy
        cannot see, and this protocol is otherwise maintained method by method.
        """
        ...

    def create_or_update_session(self, session: RuntimeSession) -> RuntimeSession: ...

    def append_message(self, message: ConversationMessage) -> ConversationMessage | None: ...

    def get_message(self, message_id: str) -> ConversationMessage | None: ...

    def update_message_metadata(self, message_id: str, metadata: dict[str, Any]) -> bool: ...

    def delete_message(self, message_id: str) -> bool: ...

    def get_or_create_conversation(
        self,
        conversation_id: str,
        *,
        session_id: str = "",
        source: str = "unknown",
    ) -> str: ...

    def list_messages(
        self,
        conversation_id: str | None = None,
        *,
        limit: int = 500,
        offset: int = 0,
        actor_type: str | None = None,
        after_message_id: str | None = None,
        include_progress: bool = True,
        newest_first: bool = False,
    ) -> list[ConversationMessage]: ...

    def list_owner_messages(
        self,
        *,
        limit: int = 10000,
        after_message_id: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
    ) -> list[ConversationMessage]: ...

    def list_chat_history_messages(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        created_from: str | None = None,
        created_to: str | None = None,
        after_message_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[ConversationMessage]: ...

    def count_messages(
        self,
        conversation_id: str | None = None,
        *,
        after_message_id: str | None = None,
    ) -> int: ...

    def delete_messages(self, conversation_id: str) -> int: ...

    def delete_owner_messages(
        self,
        *,
        preserve_message_ids: Iterable[str] = (),
        before_message_id: str = "",
    ) -> int: ...

    def clear_conversation(self, conversation_id: str) -> None: ...

    def upsert_context_summary(self, summary: ContextSummary) -> ContextSummary: ...

    def get_latest_context_summary(self, conversation_id: str) -> ContextSummary | None: ...

    def _commit_context_consolidation(
        self,
        lease: TaskRecord | None,
        *,
        status: TaskStatus = "running",
        task_input: dict[str, Any] | None = None,
        result: str = "",
        expected_cursor: str = "",
        summary: ContextSummary | None = None,
    ) -> bool: ...

    def sum_usage_stats(self, *, session_id: str | None = None) -> dict[str, Any]: ...

    def export_messages_jsonl(self, path: pathlib.Path, *, conversation_id: str | None = None) -> int: ...

    def create_task(self, request: TaskRequest, *, status: TaskStatus = "queued") -> TaskRecord: ...

    def upsert_task(self, task: TaskRecord) -> TaskRecord: ...

    def claim_next_task(
        self,
        worker_id: int = 0,
        *,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
    ) -> TaskRecord | None: ...

    def heartbeat_task(self, task_id: str, *, phase: str = "") -> TaskRecord | None: ...

    def complete_task(
        self, task_id: str, *, result: str = "", result_ref: str = "", **fields: Any
    ) -> TaskRecord | None: ...

    def fail_task(
        self,
        task_id: str,
        *,
        result: str = "",
        error_code: str = "",
        status: TaskStatus = "failed",
        **fields: Any,
    ) -> TaskRecord | None: ...

    def cancel_task(self, task_id: str, *, result: str = "Task cancelled.") -> TaskRecord | None: ...

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def list_tasks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
        activity_first: bool = False,
    ) -> list[TaskRecord]: ...

    def record_usage(self, usage: LlmUsageRecord) -> LlmUsageRecord: ...

    def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    def list_audit_events(
        self,
        *,
        limit: int = 500,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        log_stream: str | None = None,
    ) -> list[AuditEvent]: ...

    def record_tool_invocation(self, invocation: ToolInvocation) -> ToolInvocation: ...

    def list_tool_invocations(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[ToolInvocation]: ...

    def record_task_episode(self, episode: TaskEpisode) -> TaskEpisode: ...

    def list_task_episodes(
        self,
        *,
        conversation_id: str | None = None,
        domains: Iterable[str] | None = None,
        search_terms: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[TaskEpisode]: ...

    def cleanup_expired_working_retrieval(
        self,
        *,
        now_iso: str,
        limit: int = 5000,
    ) -> dict[str, int]: ...

    def load_owner_runtime_state(self, tenant_id: str = "") -> dict[str, Any] | None: ...

    def save_owner_runtime_state(self, payload: dict[str, Any], tenant_id: str = "") -> None: ...


__all__ = ["RuntimeStore"]
