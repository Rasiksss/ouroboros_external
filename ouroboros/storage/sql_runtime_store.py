"""SQLAlchemy-backed runtime metadata store (SQLite and PostgreSQL)."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.storage.blob.keys import storage_tenant_id
from ouroboros.storage.db_url import postgres_failover_creator
from ouroboros.storage.dialect import claim_select_sql, insert_conversation_sql, insert_message_sql
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.migrations.runner import upgrade_head
from ouroboros.storage.runtime_backend import MessageAnchorMissingError
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
from ouroboros.storage.runtime_serialization import (
    dump_json as _dump_json,
    load_json as _load_json,
)
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def _load_json_list(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _chat_history_visible(metadata: dict[str, Any]) -> bool:
    return not bool(metadata.get("is_progress"))


class SqlRuntimeStore:
    """Transactional runtime metadata repository."""

    def __init__(self, engine: Engine, *, db_url: str, db_urls: list[str] | None = None):
        self._engine = engine
        self.db_url = db_url
        self.db_urls = list(db_urls or [db_url])
        self._dialect = engine.dialect.name
        self._lock = threading.RLock()
        with self._lock:
            upgrade_head(engine)

    @property
    def engine(self) -> Engine:
        """Underlying SQLAlchemy engine (used by core_memory infra adapters)."""
        return self._engine

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._engine.connect() as conn:
            yield conn

    def schema_version(self) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        if row is None:
            return 0
        revision = str(row[0])
        digits = "".join(ch for ch in revision if ch.isdigit())
        return int(digits or "0")

    def create_or_update_session(self, session: RuntimeSession) -> RuntimeSession:
        now = utc_now_iso()
        if not session.created_at:
            session.created_at = now
        session.last_seen_at = session.last_seen_at or now
        payload = session.model_dump(mode="json")
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, source, owner_external_id, auth_context_hash,
                        status, created_at, last_seen_at, expires_at, payload_json
                    ) VALUES (
                        :session_id, :source, :owner_external_id, :auth_context_hash,
                        :status, :created_at, :last_seen_at, :expires_at, :payload_json
                    )
                    ON CONFLICT(session_id) DO UPDATE SET
                        source=excluded.source,
                        owner_external_id=excluded.owner_external_id,
                        auth_context_hash=excluded.auth_context_hash,
                        status=excluded.status,
                        last_seen_at=excluded.last_seen_at,
                        expires_at=excluded.expires_at,
                        payload_json=excluded.payload_json
                    """
                ),
                {
                    "session_id": session.session_id,
                    "source": session.source,
                    "owner_external_id": session.owner_external_id,
                    "auth_context_hash": session.auth_context_hash,
                    "status": session.status,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                    "payload_json": _dump_json(payload),
                },
            )
            conn.commit()
        return session

    def append_message(self, message: ConversationMessage) -> ConversationMessage | None:
        now = utc_now_iso()
        if not message.created_at:
            message.created_at = now
        if not message.message_id:
            message.message_id = uuid.uuid4().hex
        payload = message.model_dump(mode="json")
        content_value = encrypt_field(message.content, dialect_name=self._dialect)
        metadata_json = encrypt_field(_dump_json(message.metadata), dialect_name=self._dialect)
        payload_json = encrypt_field(_dump_json(payload), dialect_name=self._dialect)
        with self._lock, self._connection() as conn:
            conn.execute(
                text(insert_conversation_sql(self._dialect)),
                {
                    "conversation_id": message.conversation_id,
                    "session_id": message.session_id,
                    "source": message.source,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.execute(
                text("UPDATE conversations SET updated_at=:updated_at WHERE conversation_id=:conversation_id"),
                {"updated_at": now, "conversation_id": message.conversation_id},
            )
            insert_result = conn.execute(
                text(insert_message_sql(self._dialect)),
                {
                    "message_id": message.message_id,
                    "conversation_id": message.conversation_id,
                    "session_id": message.session_id,
                    "source": message.source,
                    "source_message_id": message.source_message_id,
                    "actor_type": message.actor_type,
                    "channel": message.channel,
                    "content": content_value,
                    "content_ref": message.content_ref,
                    "content_hash": message.content_hash,
                    "created_at": message.created_at,
                    "metadata_json": metadata_json,
                    "payload_json": payload_json,
                    "chat_history_visible": _chat_history_visible(message.metadata),
                },
            )
            conn.commit()
        if insert_result.rowcount == 0:
            return None
        return message

    def create_task(self, request: TaskRequest, *, status: TaskStatus = "queued") -> TaskRecord:
        task_id = request.id or uuid.uuid4().hex[:8]
        now = utc_now_iso()
        if not request.queued_at:
            request.queued_at = now
        if not request.id:
            request.id = task_id
        task = TaskRecord(
            task_id=task_id,
            conversation_id=request.conversation_id,
            source=request.source,
            correlation_id=request.correlation_id,
            status=status,
            task_type=request.type,
            chat_id=request.chat_id,
            input=request.to_task_dict(),
            priority=request.priority,
            attempt=max(1, int(request.attempt or 1)),
            queued_at=request.queued_at,
            created_at=now,
        )
        return self.upsert_task(task)

    def upsert_task(self, task: TaskRecord) -> TaskRecord:
        if not task.created_at:
            task.created_at = utc_now_iso()
        params = dict(zip(self._task_param_names(), self._task_params(task), strict=True))
        with self._lock, self._connection() as conn:
            write_result = conn.execute(
                text(
                    """
                    INSERT INTO tasks (
                        task_id, session_id, conversation_id, source, correlation_id,
                        status, task_type, chat_id, input_json, input_ref, result,
                        result_ref, error_code, priority, attempt, worker_id, queued_at,
                        created_at, started_at, heartbeat_at, finished_at,
                        metadata_json, payload_json
                    ) VALUES (
                        :task_id, :session_id, :conversation_id, :source, :correlation_id,
                        :status, :task_type, :chat_id, :input_json, :input_ref, :result,
                        :result_ref, :error_code, :priority, :attempt, :worker_id, :queued_at,
                        :created_at, :started_at, :heartbeat_at, :finished_at,
                        :metadata_json, :payload_json
                    )
                    ON CONFLICT(task_id) DO UPDATE SET
                        session_id=excluded.session_id,
                        conversation_id=excluded.conversation_id,
                        source=excluded.source,
                        correlation_id=excluded.correlation_id,
                        status=excluded.status,
                        task_type=excluded.task_type,
                        chat_id=excluded.chat_id,
                        input_json=excluded.input_json,
                        input_ref=excluded.input_ref,
                        result=excluded.result,
                        result_ref=excluded.result_ref,
                        error_code=excluded.error_code,
                        priority=excluded.priority,
                        attempt=excluded.attempt,
                        worker_id=excluded.worker_id,
                        queued_at=excluded.queued_at,
                        started_at=excluded.started_at,
                        heartbeat_at=excluded.heartbeat_at,
                        finished_at=excluded.finished_at,
                        metadata_json=excluded.metadata_json,
                        payload_json=excluded.payload_json
                    WHERE tasks.task_type != 'context_consolidation'
                       OR tasks.status NOT IN
                          ('completed', 'rejected_duplicate', 'failed', 'cancelled', 'budget_exceeded')
                    """
                ),
                params,
            )
            stored = None
            if write_result.rowcount == 0:
                stored = conn.execute(
                    text("SELECT * FROM tasks WHERE task_id=:task_id"), {"task_id": task.task_id}
                ).fetchone()
            conn.commit()
        return self._row_to_task(stored) if stored else task

    @staticmethod
    def _task_param_names() -> tuple[str, ...]:
        return (
            "task_id",
            "session_id",
            "conversation_id",
            "source",
            "correlation_id",
            "status",
            "task_type",
            "chat_id",
            "input_json",
            "input_ref",
            "result",
            "result_ref",
            "error_code",
            "priority",
            "attempt",
            "worker_id",
            "queued_at",
            "created_at",
            "started_at",
            "heartbeat_at",
            "finished_at",
            "metadata_json",
            "payload_json",
        )

    def _task_type_claim_filter(
        self,
        *,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        clauses: list[str] = []
        params: dict[str, str] = {}
        include = sorted({str(t or "").strip() for t in (include_task_types or []) if str(t or "").strip()})
        exclude = sorted({str(t or "").strip() for t in (exclude_task_types or []) if str(t or "").strip()})
        if include:
            names = []
            for idx, value in enumerate(include):
                key = f"include_task_type_{idx}"
                names.append(f":{key}")
                params[key] = value
            clauses.append(f"task_type IN ({', '.join(names)})")
        if exclude:
            names = []
            for idx, value in enumerate(exclude):
                key = f"exclude_task_type_{idx}"
                names.append(f":{key}")
                params[key] = value
            clauses.append(f"(task_type IS NULL OR task_type NOT IN ({', '.join(names)}))")
        return " AND ".join(clauses), params

    def claim_next_task(
        self,
        worker_id: int = 0,
        *,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
    ) -> TaskRecord | None:
        now = utc_now_iso()
        extra_where, params = self._task_type_claim_filter(
            include_task_types=include_task_types,
            exclude_task_types=exclude_task_types,
        )
        with self._lock, self._connection() as conn:
            if self._dialect == "sqlite":
                conn.execute(text("BEGIN IMMEDIATE"))
            else:
                conn.execute(text("BEGIN"))
            row = conn.execute(text(claim_select_sql(self._dialect, extra_where)), params).fetchone()
            if row is None:
                conn.execute(text("COMMIT"))
                return None
            task = self._row_to_task(row)
            task.status = "running"
            task.worker_id = worker_id
            task.started_at = now
            task.heartbeat_at = now
            task_payload = _dump_json(task.model_dump(mode="json"))
            if task.task_type == "context_consolidation":
                task_payload = encrypt_field(task_payload, dialect_name=self._dialect)
            conn.execute(
                text(
                    """
                    UPDATE tasks SET status=:status, worker_id=:worker_id,
                        started_at=:started_at, heartbeat_at=:heartbeat_at, payload_json=:payload_json
                    WHERE task_id=:task_id AND status IN ('queued', 'scheduled', 'interrupted')
                    """
                ),
                {
                    "status": task.status,
                    "worker_id": worker_id,
                    "started_at": now,
                    "heartbeat_at": now,
                    "payload_json": task_payload,
                    "task_id": task.task_id,
                },
            )
            conn.execute(text("COMMIT"))
        return task

    def heartbeat_task(self, task_id: str, *, phase: str = "") -> TaskRecord | None:
        """Record liveness of a running task; never a status transition in either direction."""
        metadata = {"heartbeat_phase": phase} if phase else {}
        return self.update_task_status(task_id, "running", metadata=metadata, heartbeat=True)

    def complete_task(
        self, task_id: str, *, result: str = "", result_ref: str = "", **fields: Any
    ) -> TaskRecord | None:
        return self.update_task_status(task_id, "completed", result=result, result_ref=result_ref, **fields)

    def fail_task(
        self,
        task_id: str,
        *,
        result: str = "",
        error_code: str = "",
        status: TaskStatus = "failed",
        **fields: Any,
    ) -> TaskRecord | None:
        return self.update_task_status(task_id, status, result=result, error_code=error_code, **fields)

    def cancel_task(self, task_id: str, *, result: str = "Task cancelled.") -> TaskRecord | None:
        return self.update_task_status(task_id, "cancelled", result=result)

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        heartbeat: bool = False,
        **fields: Any,
    ) -> TaskRecord | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        from ouroboros.storage.task_results import TERMINAL_STATUSES

        # A heartbeat only records liveness: it never moves a record into `running`, and
        # never revives one that already left it.
        if heartbeat and task.status != "running":
            return task
        # A terminal outcome is absorbing, so a zombie body can neither resurrect the record
        # nor overwrite the result that killed it. Re-writing the same status still lands.
        if task.status in TERMINAL_STATUSES and status != task.status:
            log.debug(
                "Refusing task status transition %s -> %s for %s: terminal status is absorbing",
                task.status,
                status,
                task_id,
            )
            return task
        task.status = status
        now = utc_now_iso()
        if heartbeat:
            task.heartbeat_at = now
        if status == "running" and not task.started_at:
            task.started_at = now
        if status in {"completed", "rejected_duplicate", "failed", "cancelled", "budget_exceeded"}:
            task.finished_at = now
        if result is not None:
            task.result = result
        if result_ref is not None:
            task.result_ref = result_ref
        if error_code is not None:
            task.error_code = error_code
        if metadata:
            task.metadata.update(metadata)
        for key, value in fields.items():
            if hasattr(task, key):
                setattr(task, key, value)
            else:
                task.metadata[key] = value
        return self.upsert_task(task)

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                text("SELECT * FROM tasks WHERE task_id=:task_id"),
                {"task_id": str(task_id)},
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
        activity_first: bool = False,
    ) -> list[TaskRecord]:
        wanted = [str(status) for status in (statuses or []) if str(status).strip()]
        params: dict[str, Any] = {"limit": int(limit)}
        order = (
            "COALESCE(NULLIF(finished_at, ''), NULLIF(started_at, ''), created_at) DESC, task_id DESC"
            if activity_first
            else "created_at ASC, task_id ASC"
        )
        if wanted:
            placeholders = ", ".join(f":status_{index}" for index, _ in enumerate(wanted))
            where = f"WHERE status IN ({placeholders})"
            for index, status in enumerate(wanted):
                params[f"status_{index}"] = status
        else:
            where = ""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM tasks {where} ORDER BY {order} LIMIT :limit"),
                params,
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def record_usage(self, usage: LlmUsageRecord) -> LlmUsageRecord:
        if not usage.usage_id:
            usage.usage_id = uuid.uuid4().hex
        if not usage.created_at:
            usage.created_at = utc_now_iso()
        payload = usage.model_dump(mode="json")
        session_id = str(usage.metadata.get("session_id") or "")
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO llm_usage (
                        usage_id, task_id, session_id, provider, model, prompt_tokens,
                        completion_tokens, cached_tokens, created_at,
                        metadata_json, payload_json
                    ) VALUES (
                        :usage_id, :task_id, :session_id, :provider, :model, :prompt_tokens,
                        :completion_tokens, :cached_tokens, :created_at,
                        :metadata_json, :payload_json
                    )
                    """
                ),
                {
                    "usage_id": usage.usage_id,
                    "task_id": usage.task_id,
                    "session_id": session_id,
                    "provider": usage.provider,
                    "model": usage.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "created_at": usage.created_at,
                    "metadata_json": _dump_json(usage.metadata),
                    "payload_json": _dump_json(payload),
                },
            )
            conn.commit()
        return usage

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        if not event.event_id:
            event.event_id = uuid.uuid4().hex
        if not event.timestamp:
            event.timestamp = utc_now_iso()
        payload = event.model_dump(mode="json")
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        event_id, timestamp, correlation_id, actor_type, resource_type,
                        resource_id, event_type, log_stream, decision, reason, metadata_json, payload_json
                    ) VALUES (
                        :event_id, :timestamp, :correlation_id, :actor_type, :resource_type,
                        :resource_id, :event_type, :log_stream, :decision, :reason, :metadata_json, :payload_json
                    )
                    """
                ),
                {
                    "event_id": event.event_id,
                    "timestamp": event.timestamp,
                    "correlation_id": event.correlation_id,
                    "actor_type": event.actor_type,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "event_type": event.event_type,
                    "log_stream": event.log_stream or "events",
                    "decision": event.decision,
                    "reason": event.reason,
                    "metadata_json": _dump_json(event.metadata),
                    "payload_json": _dump_json(payload),
                },
            )
            conn.commit()
        return event

    def list_audit_events(
        self,
        *,
        limit: int = 500,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        log_stream: str | None = None,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if event_type:
            clauses.append("event_type = :event_type")
            params["event_type"] = event_type
        if resource_type:
            clauses.append("resource_type = :resource_type")
            params["resource_type"] = resource_type
        if resource_id:
            clauses.append("resource_id = :resource_id")
            params["resource_id"] = resource_id
        if log_stream:
            clauses.append("log_stream = :log_stream")
            params["log_stream"] = str(log_stream)
        params["limit"] = max(1, int(limit or 1))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM audit_events {where}
                    ORDER BY timestamp DESC, event_id DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).fetchall()
        events = [self._row_to_audit_event(row) for row in rows]
        return list(reversed(events))

    def _resolve_tenant_id(self, tenant_id: str) -> str:
        text = str(tenant_id or "").strip()
        return text or storage_tenant_id()

    def load_owner_runtime_state(self, tenant_id: str = "") -> dict[str, Any] | None:
        tid = self._resolve_tenant_id(tenant_id)
        with self._lock, self._connection() as conn:
            row = conn.execute(
                text("SELECT payload_json FROM owner_runtime_state WHERE tenant_id = :tenant_id"),
                {"tenant_id": tid},
            ).fetchone()
        if row is None:
            return None
        data = _load_json(str(row[0]))
        return data if isinstance(data, dict) else None

    def save_owner_runtime_state(self, payload: dict[str, Any], tenant_id: str = "") -> None:
        tid = self._resolve_tenant_id(tenant_id)
        now = utc_now_iso()
        body = _dump_json(payload if isinstance(payload, dict) else {})
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO owner_runtime_state (tenant_id, payload_json, updated_at)
                    VALUES (:tenant_id, :payload_json, :updated_at)
                    ON CONFLICT(tenant_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """
                ),
                {"tenant_id": tid, "payload_json": body, "updated_at": now},
            )
            conn.commit()

    def record_tool_invocation(self, invocation: ToolInvocation) -> ToolInvocation:
        invocation.tool_invocation_id = invocation.tool_invocation_id or uuid.uuid4().hex
        invocation.created_at = invocation.created_at or utc_now_iso()
        payload = invocation.model_dump(mode="json")
        params = {
            "tool_invocation_id": invocation.tool_invocation_id,
            "task_id": invocation.task_id,
            "conversation_id": invocation.conversation_id,
            "tool_name": invocation.tool_name,
            "status": invocation.status,
            "args_redacted_json": encrypt_field(_dump_json(invocation.args_redacted), dialect_name=self._dialect),
            "result_summary_json": encrypt_field(_dump_json(invocation.result_summary), dialect_name=self._dialect),
            "result_index_json": _dump_json(invocation.result_index),
            "search_terms_json": _dump_json(invocation.search_terms),
            "payload_json": encrypt_field(_dump_json(payload), dialect_name=self._dialect),
            "payload_hash": invocation.payload_hash,
            "sensitivity": invocation.sensitivity,
            "retention_class": invocation.retention_class,
            "created_at": invocation.created_at,
            "expires_at": invocation.expires_at,
        }
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tool_invocations (
                        tool_invocation_id, task_id, conversation_id, tool_name, status,
                        args_redacted_json, result_summary_json, result_index_json,
                        search_terms_json, payload_json, payload_hash, sensitivity,
                        retention_class, created_at, expires_at
                    ) VALUES (
                        :tool_invocation_id, :task_id, :conversation_id, :tool_name, :status,
                        :args_redacted_json, :result_summary_json, :result_index_json,
                        :search_terms_json, :payload_json, :payload_hash, :sensitivity,
                        :retention_class, :created_at, :expires_at
                    )
                    ON CONFLICT(tool_invocation_id) DO UPDATE SET
                        task_id=excluded.task_id,
                        conversation_id=excluded.conversation_id,
                        tool_name=excluded.tool_name,
                        status=excluded.status,
                        args_redacted_json=excluded.args_redacted_json,
                        result_summary_json=excluded.result_summary_json,
                        result_index_json=excluded.result_index_json,
                        search_terms_json=excluded.search_terms_json,
                        payload_json=excluded.payload_json,
                        payload_hash=excluded.payload_hash,
                        sensitivity=excluded.sensitivity,
                        retention_class=excluded.retention_class,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at
                    """
                ),
                params,
            )
            conn.commit()
        return invocation

    def list_tool_invocations(
        self,
        *,
        task_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[ToolInvocation]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, int(limit or 1))}
        if task_id:
            clauses.append("task_id = :task_id")
            params["task_id"] = str(task_id)
        if conversation_id:
            clauses.append("conversation_id = :conversation_id")
            params["conversation_id"] = str(conversation_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM tool_invocations {where} ORDER BY created_at DESC, tool_invocation_id DESC LIMIT :limit"
        with self._lock, self._connection() as conn:
            rows = conn.execute(text(query), params).fetchall()
        return [self._row_to_tool_invocation(row) for row in rows]

    def record_task_episode(self, episode: TaskEpisode) -> TaskEpisode:
        episode.episode_id = episode.episode_id or uuid.uuid4().hex
        episode.created_at = episode.created_at or utc_now_iso()
        payload = episode.model_dump(mode="json")
        params = {
            "episode_id": episode.episode_id,
            "task_id": episode.task_id,
            "conversation_id": episode.conversation_id,
            "created_at": episode.created_at,
            "user_text": encrypt_field(episode.user_text, dialect_name=self._dialect),
            "answer_preview": encrypt_field(episode.answer_preview, dialect_name=self._dialect),
            "tools_used_json": _dump_json(episode.tools_used),
            "domains_json": _dump_json(episode.domains),
            "entities_json": encrypt_field(_dump_json(episode.entities), dialect_name=self._dialect),
            "refs_json": encrypt_field(_dump_json(episode.refs), dialect_name=self._dialect),
            "search_terms_json": _dump_json(episode.search_terms),
            "payload_json": encrypt_field(_dump_json(payload), dialect_name=self._dialect),
            "expires_at": episode.expires_at,
        }
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO task_episodes (
                        episode_id, task_id, conversation_id, created_at, user_text,
                        answer_preview, tools_used_json, domains_json, entities_json,
                        refs_json, search_terms_json, payload_json, expires_at
                    ) VALUES (
                        :episode_id, :task_id, :conversation_id, :created_at, :user_text,
                        :answer_preview, :tools_used_json, :domains_json, :entities_json,
                        :refs_json, :search_terms_json, :payload_json, :expires_at
                    )
                    ON CONFLICT(episode_id) DO UPDATE SET
                        task_id=excluded.task_id,
                        conversation_id=excluded.conversation_id,
                        created_at=excluded.created_at,
                        user_text=excluded.user_text,
                        answer_preview=excluded.answer_preview,
                        tools_used_json=excluded.tools_used_json,
                        domains_json=excluded.domains_json,
                        entities_json=excluded.entities_json,
                        refs_json=excluded.refs_json,
                        search_terms_json=excluded.search_terms_json,
                        payload_json=excluded.payload_json,
                        expires_at=excluded.expires_at
                    """
                ),
                params,
            )
            conn.commit()
        return episode

    def list_task_episodes(
        self,
        *,
        conversation_id: str | None = None,
        domains: Iterable[str] | None = None,
        search_terms: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[TaskEpisode]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, int(limit or 1))}
        if conversation_id:
            clauses.append("conversation_id = :conversation_id")
            params["conversation_id"] = str(conversation_id)
        self._append_json_search_clause(
            clauses,
            params,
            column="domains_json",
            prefix="domain",
            values=domains,
            max_values=8,
        )
        self._append_json_search_clause(
            clauses,
            params,
            column="search_terms_json",
            prefix="term",
            values=search_terms,
            max_values=12,
        )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM task_episodes {where} ORDER BY created_at DESC, episode_id DESC LIMIT :limit"
        with self._lock, self._connection() as conn:
            rows = conn.execute(text(query), params).fetchall()
        return [self._row_to_task_episode(row) for row in rows]

    @staticmethod
    def _append_json_search_clause(
        clauses: list[str],
        params: dict[str, Any],
        *,
        column: str,
        prefix: str,
        values: Iterable[str] | None,
        max_values: int,
    ) -> None:
        normalized = [str(value).strip().casefold() for value in (values or []) if str(value).strip()][:max_values]
        if not normalized:
            return
        predicates = []
        for index, value in enumerate(normalized):
            key = f"{prefix}_{index}"
            predicates.append(f"LOWER({column}) LIKE :{key}")
            params[key] = f"%{value}%"
        clauses.append(f"({' OR '.join(predicates)})")

    def cleanup_expired_working_retrieval(
        self,
        *,
        now_iso: str,
        limit: int = 5000,
    ) -> dict[str, int]:
        now = str(now_iso or utc_now_iso())
        capped = max(1, int(limit or 1))
        removed: dict[str, int] = {}
        with self._lock, self._connection() as conn:
            for table, id_column in (
                ("task_episodes", "episode_id"),
                ("tool_invocations", "tool_invocation_id"),
            ):
                rows = conn.execute(
                    text(f"SELECT {id_column} FROM {table} WHERE expires_at != '' AND expires_at <= :now LIMIT :limit"),
                    {"now": now, "limit": capped},
                ).fetchall()
                ids = [str(row[0]) for row in rows]
                if not ids:
                    removed[table] = 0
                    continue
                id_params = {f"id_{index}": value for index, value in enumerate(ids)}
                placeholders = ", ".join(f":id_{index}" for index in range(len(ids)))
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})"),
                    id_params,
                )
                removed[table] = int(result.rowcount or 0)
            conn.commit()
        return removed

    def get_or_create_conversation(
        self,
        conversation_id: str,
        *,
        session_id: str = "",
        source: str = "unknown",
    ) -> str:
        now = utc_now_iso()
        with self._lock, self._connection() as conn:
            conn.execute(
                text(insert_conversation_sql(self._dialect)),
                {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE conversations SET session_id=:session_id, source=:source, updated_at=:updated_at
                    WHERE conversation_id=:conversation_id
                    """
                ),
                {
                    "session_id": session_id,
                    "source": source,
                    "updated_at": now,
                    "conversation_id": conversation_id,
                },
            )
            conn.commit()
        return conversation_id

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
    ) -> list[ConversationMessage]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if not include_progress:
            # The same indexed column ``list_chat_history_messages`` reads, rather than a
            # second implementation of the predicate in Python — and applied before the
            # LIMIT, so a burst of progress lines cannot crowd the window.
            clauses.append("chat_history_visible = :visible")
            params["visible"] = True
        if conversation_id:
            clauses.append("conversation_id = :conversation_id")
            params["conversation_id"] = conversation_id
        if actor_type:
            clauses.append("actor_type = :actor_type")
            params["actor_type"] = actor_type
        if after_message_id:
            anchor = self.get_message(after_message_id)
            if anchor is not None:
                clauses.append(
                    "(created_at > :anchor_created_at OR (created_at = :anchor_created_at AND message_id > :anchor_message_id))"
                )
                params["anchor_created_at"] = anchor.created_at
                params["anchor_message_id"] = anchor.message_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "DESC" if newest_first else "ASC"
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM messages {where}
                    ORDER BY created_at {order}, message_id {order}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).fetchall()
        if newest_first:
            # The window is cut from the newest rows; the caller still reads them
            # in the order they were said.
            rows = list(reversed(rows))
        return [self._row_to_message(row) for row in rows]

    def list_owner_messages(
        self,
        *,
        limit: int = 10000,
        after_message_id: str | None = None,
        include_progress: bool = False,
        newest_first: bool = False,
    ) -> list[ConversationMessage]:
        """The owner's own view. Progress lines are that view of their agent working, so the
        ones produced while serving a remote peer stay out even when progress is asked for.

        ``newest_first`` decides which end the LIMIT cuts from, not the order of the
        result: a bounded read of a long history wants the newest rows, and every
        caller still reads them oldest-first."""
        messages = self.list_messages(
            None,
            limit=limit,
            after_message_id=after_message_id,
            include_progress=include_progress,
            newest_first=newest_first,
        )
        if not include_progress:
            return messages
        return [
            message
            for message in messages
            if not (bool(message.metadata.get("is_progress")) and is_internal_chat_id(message.metadata.get("chat_id")))
        ]

    def list_chat_history_messages(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        created_from: str | None = None,
        created_to: str | None = None,
        after_message_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[ConversationMessage]:
        clean_limit = max(0, int(limit))
        if clean_limit == 0:
            return []
        clauses = ["chat_history_visible = :visible"]
        params: dict[str, Any] = {
            "visible": True,
            "limit": clean_limit,
            "offset": max(0, int(offset)),
        }
        if created_from:
            clauses.append("created_at >= :created_from")
            params["created_from"] = str(created_from)
        if created_to:
            clauses.append("created_at < :created_to")
            params["created_to"] = str(created_to)
        if after_message_id:
            anchor = self.get_message(after_message_id)
            if anchor is None:
                raise MessageAnchorMissingError(after_message_id)
            clauses.append(
                "(created_at > :anchor_created_at OR (created_at = :anchor_created_at AND message_id > :anchor_message_id))"
            )
            params["anchor_created_at"] = anchor.created_at
            params["anchor_message_id"] = anchor.message_id
        where = " AND ".join(clauses)
        order = "ASC" if oldest_first else "DESC"
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM messages
                    WHERE {where}
                    ORDER BY created_at {order}, message_id {order}
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_message(self, message_id: str) -> ConversationMessage | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                text("SELECT * FROM messages WHERE message_id=:message_id"),
                {"message_id": str(message_id)},
            ).fetchone()
        return self._row_to_message(row) if row else None

    def update_message_metadata(self, message_id: str, metadata: dict[str, Any]) -> bool:
        message = self.get_message(message_id)
        if message is None:
            return False
        message.metadata = dict(metadata)
        metadata_json = encrypt_field(_dump_json(message.metadata), dialect_name=self._dialect)
        payload_json = encrypt_field(
            _dump_json(message.model_dump(mode="json")),
            dialect_name=self._dialect,
        )
        with self._lock, self._connection() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE messages
                    SET metadata_json=:metadata_json,
                        payload_json=:payload_json,
                        chat_history_visible=:chat_history_visible
                    WHERE message_id=:message_id
                    """
                ),
                {
                    "message_id": str(message_id),
                    "metadata_json": metadata_json,
                    "payload_json": payload_json,
                    "chat_history_visible": _chat_history_visible(message.metadata),
                },
            )
            conn.commit()
        return bool(result.rowcount)

    def count_messages(
        self,
        conversation_id: str | None = None,
        *,
        after_message_id: str | None = None,
    ) -> int:
        return len(
            self.list_messages(
                conversation_id,
                limit=1_000_000,
                after_message_id=after_message_id,
            )
        )

    def delete_message(self, message_id: str) -> bool:
        with self._lock, self._connection() as conn:
            result = conn.execute(
                text("DELETE FROM messages WHERE message_id=:message_id"),
                {"message_id": str(message_id)},
            )
            conn.commit()
        return bool(result.rowcount)

    def delete_messages(self, conversation_id: str) -> int:
        with self._lock, self._connection() as conn:
            result = conn.execute(
                text("DELETE FROM messages WHERE conversation_id=:conversation_id"),
                {"conversation_id": conversation_id},
            )
            conn.execute(
                text("DELETE FROM context_summaries WHERE conversation_id=:conversation_id"),
                {"conversation_id": conversation_id},
            )
            conn.commit()
        return int(result.rowcount or 0)

    def delete_owner_messages(
        self,
        *,
        preserve_message_ids: Iterable[str] = (),
        before_message_id: str = "",
    ) -> int:
        removed = 0
        preserved = {str(value) for value in preserve_message_ids if str(value)}
        with self._lock, self._connection() as conn:
            cutoff = None
            if before_message_id:
                cutoff_row = conn.execute(
                    text("SELECT created_at, message_id FROM messages WHERE message_id=:message_id"),
                    {"message_id": before_message_id},
                ).fetchone()
                if cutoff_row is None:
                    raise ValueError("message deletion watermark was not found")
                cutoff = str(cutoff_row._mapping["created_at"])
            rows = conn.execute(text("SELECT message_id, created_at FROM messages")).fetchall()
            to_delete = []
            for row in rows:
                mapping = row._mapping
                message_id = str(mapping["message_id"])
                created_at = str(mapping["created_at"])
                if message_id not in preserved and (cutoff is None or created_at <= cutoff):
                    to_delete.append(message_id)
            for message_id in to_delete:
                conn.execute(text("DELETE FROM messages WHERE message_id=:message_id"), {"message_id": message_id})
                removed += 1
            conn.execute(text("DELETE FROM context_summaries"))
            conn.execute(text("DELETE FROM tasks WHERE task_type='context_consolidation'"))
            conn.commit()
        return removed

    def clear_conversation(self, conversation_id: str) -> None:
        self.delete_messages(conversation_id)

    def upsert_context_summary(self, summary: ContextSummary) -> ContextSummary:
        now = utc_now_iso()
        if not summary.created_at:
            summary.created_at = now
        if not summary.summary_id:
            summary.summary_id = uuid.uuid4().hex
        payload = summary.model_dump(mode="json")
        with self._lock, self._connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO context_summaries (
                        summary_id, conversation_id, source_offsets_ref, summary_ref,
                        token_budget, summary_hash, created_at, metadata_json, payload_json
                    ) VALUES (
                        :summary_id, :conversation_id, :source_offsets_ref, :summary_ref,
                        :token_budget, :summary_hash, :created_at, :metadata_json, :payload_json
                    )
                    ON CONFLICT(summary_id) DO UPDATE SET
                        source_offsets_ref=excluded.source_offsets_ref,
                        summary_ref=excluded.summary_ref,
                        token_budget=excluded.token_budget,
                        summary_hash=excluded.summary_hash,
                        created_at=excluded.created_at,
                        metadata_json=excluded.metadata_json,
                        payload_json=excluded.payload_json
                    """
                ),
                {
                    "summary_id": summary.summary_id,
                    "conversation_id": summary.conversation_id,
                    "source_offsets_ref": summary.source_offsets_ref,
                    "summary_ref": encrypt_field(summary.summary_ref, dialect_name=self._dialect),
                    "token_budget": int(summary.token_budget),
                    "summary_hash": summary.summary_hash,
                    "created_at": summary.created_at,
                    "metadata_json": encrypt_field(_dump_json(summary.metadata), dialect_name=self._dialect),
                    "payload_json": encrypt_field(_dump_json(payload), dialect_name=self._dialect),
                },
            )
            conn.commit()
        return summary

    def get_latest_context_summary(self, conversation_id: str) -> ContextSummary | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT * FROM context_summaries
                    WHERE conversation_id=:conversation_id
                    ORDER BY created_at DESC, summary_id DESC
                    LIMIT 1
                    """
                ),
                {"conversation_id": conversation_id},
            ).fetchone()
        return self._row_to_context_summary(row) if row else None

    def _commit_context_consolidation(
        self,
        lease: TaskRecord | None,
        *,
        status: TaskStatus = "running",
        task_input: dict[str, Any] | None = None,
        result: str = "",
        expected_cursor: str = "",
        summary: ContextSummary | None = None,
    ) -> bool:
        now = utc_now_iso()
        params: dict[str, Any] = {}
        if summary:
            summary.created_at = now
            params = {
                "summary_id": summary.summary_id,
                "conversation_id": summary.conversation_id,
                "source_offsets_ref": summary.source_offsets_ref,
                "summary_ref": encrypt_field(summary.summary_ref, dialect_name=self._dialect),
                "token_budget": int(summary.token_budget),
                "summary_hash": summary.summary_hash,
                "created_at": now,
                "metadata_json": encrypt_field(_dump_json(summary.metadata), dialect_name=self._dialect),
                "payload_json": encrypt_field(_dump_json(summary.model_dump(mode="json")), dialect_name=self._dialect),
                "expected_cursor": str(expected_cursor or ""),
            }
        with self._lock, self._connection() as conn:
            conn.execute(text("BEGIN IMMEDIATE" if self._dialect == "sqlite" else "BEGIN"))
            if lease:
                finished_at = (
                    now if status in {"completed", "failed", "cancelled", "budget_exceeded"} else lease.finished_at
                )
                saved_input = lease.input if task_input is None else task_input
                if status in {"completed", "failed", "cancelled", "budget_exceeded"}:
                    saved_input = {key: value for key, value in saved_input.items() if not key.startswith("pending_")}
                updated = lease.model_copy(
                    update={
                        "status": status,
                        "input": saved_input,
                        "result": result,
                        "worker_id": None,
                        "finished_at": finished_at,
                    }
                )
                task_result = conn.execute(
                    text("""
                    UPDATE tasks SET status=:status, input_json=:input_json, result=:result,
                        worker_id=NULL, finished_at=:finished_at, payload_json=:task_payload_json
                    WHERE task_id=:task_id AND task_type='context_consolidation' AND status='running'
                      AND attempt=:attempt AND started_at=:started_at AND worker_id=:worker_id
                """),
                    {
                        "status": status,
                        "input_json": encrypt_field(_dump_json(updated.input), dialect_name=self._dialect),
                        "result": result,
                        "finished_at": finished_at,
                        "task_payload_json": encrypt_field(
                            _dump_json(updated.model_dump(mode="json")), dialect_name=self._dialect
                        ),
                        "task_id": lease.task_id,
                        "attempt": lease.attempt,
                        "started_at": lease.started_at,
                        "worker_id": lease.worker_id,
                    },
                )
                if int(task_result.rowcount or 0) != 1:
                    conn.rollback()
                    return False
            if summary:
                summary_result = conn.execute(
                    text("""
                    UPDATE context_summaries SET
                        conversation_id=:conversation_id, source_offsets_ref=:source_offsets_ref,
                        summary_ref=:summary_ref, token_budget=:token_budget, summary_hash=:summary_hash,
                        created_at=:created_at, metadata_json=:metadata_json, payload_json=:payload_json
                    WHERE summary_id=:summary_id
                      AND COALESCE(source_offsets_ref, '')=:expected_cursor
                """),
                    params,
                )
                if not summary_result.rowcount and not expected_cursor:
                    summary_result = conn.execute(
                        text("""
                        INSERT INTO context_summaries (summary_id, conversation_id, source_offsets_ref,
                            summary_ref, token_budget, summary_hash, created_at, metadata_json, payload_json)
                        VALUES (:summary_id, :conversation_id, :source_offsets_ref, :summary_ref,
                            :token_budget, :summary_hash, :created_at, :metadata_json, :payload_json)
                        ON CONFLICT(summary_id) DO NOTHING
                    """),
                        params,
                    )
                if int(summary_result.rowcount or 0) != 1:
                    conn.rollback()
                    return False
            conn.commit()
        return True

    def sum_usage_stats(self, *, session_id: str | None = None) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if session_id:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        COUNT(*) AS spent_calls,
                        COALESCE(SUM(prompt_tokens), 0) AS spent_tokens_prompt,
                        COALESCE(SUM(completion_tokens), 0) AS spent_tokens_completion,
                        COALESCE(SUM(cached_tokens), 0) AS spent_tokens_cached
                    FROM llm_usage {where}
                    """
                ),
                params,
            ).fetchone()
        if row is None:
            return {
                "spent_calls": 0,
                "spent_tokens_prompt": 0,
                "spent_tokens_completion": 0,
                "spent_tokens_cached": 0,
            }
        return {
            "spent_calls": int(row[0] or 0),
            "spent_tokens_prompt": int(row[1] or 0),
            "spent_tokens_completion": int(row[2] or 0),
            "spent_tokens_cached": int(row[3] or 0),
        }

    def export_messages_jsonl(self, path: pathlib.Path, *, conversation_id: str | None = None) -> int:
        messages = self.list_messages(
            conversation_id,
            limit=1_000_000,
            include_progress=True,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(message.model_dump_json() + "\n")
                count += 1
        return count

    def _task_params(self, task: TaskRecord) -> tuple[Any, ...]:
        input_json = _dump_json(task.input)
        payload_json = _dump_json(task.model_dump(mode="json"))
        if task.task_type == "context_consolidation":
            input_json = encrypt_field(input_json, dialect_name=self._dialect)
            payload_json = encrypt_field(payload_json, dialect_name=self._dialect)
        return (
            task.task_id,
            task.session_id,
            task.conversation_id,
            task.source,
            task.correlation_id,
            task.status,
            task.task_type,
            None if task.chat_id is None else str(task.chat_id),
            input_json,
            task.input_ref,
            task.result,
            task.result_ref,
            task.error_code,
            int(task.priority),
            int(task.attempt),
            task.worker_id,
            task.queued_at,
            task.created_at,
            task.started_at,
            task.heartbeat_at,
            task.finished_at,
            _dump_json(task.metadata),
            payload_json,
        )

    def _row_to_message(self, row: Any) -> ConversationMessage:
        mapping = row._mapping
        content = decrypt_field(mapping["content"], dialect_name=self._dialect)
        metadata_json = decrypt_field(mapping["metadata_json"], dialect_name=self._dialect)
        payload_json = decrypt_field(mapping["payload_json"], dialect_name=self._dialect)
        payload = _load_json(payload_json)
        if isinstance(payload, dict) and payload:
            payload.setdefault("metadata", _load_json(metadata_json))
            return ConversationMessage.model_validate(payload)
        return ConversationMessage(
            message_id=mapping["message_id"],
            conversation_id=mapping["conversation_id"],
            session_id=mapping["session_id"],
            source=mapping["source"],
            source_message_id=mapping["source_message_id"],
            actor_type=mapping["actor_type"],
            channel=mapping["channel"],
            content=content,
            content_ref=mapping["content_ref"],
            content_hash=mapping["content_hash"],
            created_at=mapping["created_at"],
            metadata=_load_json(metadata_json),
        )

    def _row_to_tool_invocation(self, row: Any) -> ToolInvocation:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"], dialect_name=self._dialect))
        payload.update(
            {
                "tool_invocation_id": mapping["tool_invocation_id"],
                "task_id": mapping["task_id"],
                "conversation_id": mapping["conversation_id"],
                "tool_name": mapping["tool_name"],
                "status": mapping["status"],
                "args_redacted": _load_json(decrypt_field(mapping["args_redacted_json"], dialect_name=self._dialect)),
                "result_summary": _load_json(decrypt_field(mapping["result_summary_json"], dialect_name=self._dialect)),
                "result_index": _load_json(mapping["result_index_json"]),
                "search_terms": _load_json_list(mapping["search_terms_json"]),
                "payload_hash": mapping["payload_hash"],
                "sensitivity": mapping["sensitivity"],
                "retention_class": mapping["retention_class"],
                "created_at": mapping["created_at"],
                "expires_at": mapping["expires_at"],
            }
        )
        return ToolInvocation.model_validate(payload)

    def _row_to_task_episode(self, row: Any) -> TaskEpisode:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"], dialect_name=self._dialect))
        payload.update(
            {
                "episode_id": mapping["episode_id"],
                "task_id": mapping["task_id"],
                "conversation_id": mapping["conversation_id"],
                "created_at": mapping["created_at"],
                "user_text": decrypt_field(mapping["user_text"], dialect_name=self._dialect),
                "answer_preview": decrypt_field(mapping["answer_preview"], dialect_name=self._dialect),
                "tools_used": _load_json_list(mapping["tools_used_json"]),
                "domains": _load_json_list(mapping["domains_json"]),
                "entities": _load_json(decrypt_field(mapping["entities_json"], dialect_name=self._dialect)),
                "refs": _load_json(decrypt_field(mapping["refs_json"], dialect_name=self._dialect)),
                "search_terms": _load_json_list(mapping["search_terms_json"]),
                "expires_at": mapping["expires_at"],
            }
        )
        return TaskEpisode.model_validate(payload)

    def _row_to_context_summary(self, row: Any) -> ContextSummary:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"], dialect_name=self._dialect))
        if isinstance(payload, dict) and payload:
            payload.setdefault(
                "metadata", _load_json(decrypt_field(mapping["metadata_json"], dialect_name=self._dialect))
            )
            return ContextSummary.model_validate(payload)
        return ContextSummary(
            summary_id=mapping["summary_id"],
            conversation_id=mapping["conversation_id"],
            source_offsets_ref=mapping["source_offsets_ref"],
            summary_ref=decrypt_field(mapping["summary_ref"], dialect_name=self._dialect),
            token_budget=int(mapping["token_budget"] or 0),
            summary_hash=mapping["summary_hash"],
            created_at=mapping["created_at"],
            metadata=_load_json(decrypt_field(mapping["metadata_json"], dialect_name=self._dialect)),
        )

    def _row_to_audit_event(self, row: Any) -> AuditEvent:
        mapping = row._mapping
        metadata = _load_json(mapping["metadata_json"])
        payload = _load_json(mapping["payload_json"])
        if isinstance(payload, dict) and payload:
            payload.setdefault("metadata", metadata)
            # The column is authoritative: payload_json of rows written before
            # the 0005 migration carries no log_stream.
            payload["log_stream"] = mapping["log_stream"]
            return AuditEvent.model_validate(payload)
        return AuditEvent(
            event_id=mapping["event_id"],
            timestamp=mapping["timestamp"],
            correlation_id=mapping["correlation_id"],
            actor_type=mapping["actor_type"],
            resource_type=mapping["resource_type"],
            resource_id=mapping["resource_id"],
            event_type=mapping["event_type"],
            log_stream=mapping["log_stream"],
            decision=mapping["decision"],
            reason=mapping["reason"],
            metadata=metadata,
        )

    def _row_to_task(self, row: Any) -> TaskRecord:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"], dialect_name=self._dialect))
        if isinstance(payload, dict) and payload:
            payload.setdefault("input", _load_json(decrypt_field(mapping["input_json"], dialect_name=self._dialect)))
            payload.setdefault("metadata", _load_json(mapping["metadata_json"]))
            return TaskRecord.model_validate(payload)
        return TaskRecord(
            task_id=mapping["task_id"],
            session_id=mapping["session_id"],
            conversation_id=mapping["conversation_id"],
            source=mapping["source"],
            correlation_id=mapping["correlation_id"],
            status=mapping["status"],
            task_type=mapping["task_type"],
            chat_id=mapping["chat_id"],
            input=_load_json(decrypt_field(mapping["input_json"], dialect_name=self._dialect)),
            input_ref=mapping["input_ref"],
            result=mapping["result"],
            result_ref=mapping["result_ref"],
            error_code=mapping["error_code"],
            priority=mapping["priority"],
            attempt=mapping["attempt"],
            worker_id=mapping["worker_id"],
            queued_at=mapping["queued_at"],
            created_at=mapping["created_at"],
            started_at=mapping["started_at"],
            heartbeat_at=mapping["heartbeat_at"],
            finished_at=mapping["finished_at"],
            metadata=_load_json(mapping["metadata_json"]),
        )


#: How long a SQLite writer waits for the write lock before giving up. pysqlite
#: defaults to 5 seconds, which is short: ``core_memory``'s ``revise`` holds
#: ``BEGIN IMMEDIATE`` across a Python callback, and the memory and scratchpad
#: adapters share this engine without taking the store's process-local lock, so
#: a distillation running beside a task really does contend. The loser of that
#: race raised ``database is locked`` and ``memory_save`` reported a warning the
#: model could do nothing with — the fact was simply gone.
_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _register_sqlite_pragmas(engine: Engine) -> None:
    """Give every SQLite connection a real busy timeout, and WAL where possible.

    Set per connection because both settings are connection-scoped on the
    pysqlite driver; ``journal_mode`` itself is persistent in the file, so
    re-issuing it costs one statement and keeps a freshly created database
    correct. WAL is best-effort: it is unavailable on some filesystems and the
    rollback journal still works there, whereas the timeout must always apply.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                log.debug("SQLite WAL unavailable for the runtime DB; keeping the rollback journal", exc_info=True)
        finally:
            cursor.close()


def _register_disconnect_handler(engine: Engine) -> None:
    @event.listens_for(engine, "handle_error")
    def _mark_disconnect(context: Any) -> None:
        exc = context.original_exception
        if exc is None:
            return
        if isinstance(exc, DBAPIError) and exc.connection_invalidated:
            context.is_disconnect = True
            return
        if isinstance(exc, (OSError, ConnectionError)):
            context.is_disconnect = True


def create_runtime_engine(db_urls: str | list[str], *, poolclass: type[Any] | None = None) -> Engine:
    urls = [db_urls] if isinstance(db_urls, str) else [str(url) for url in db_urls if str(url).strip()]
    if not urls:
        raise ValueError("at least one runtime DB URL is required")
    if len(urls) > 1 and any(url.startswith("sqlite") for url in urls):
        raise ValueError("SQLite runtime DB supports only a single connection URL")

    engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if poolclass is not None:
        engine_kwargs["poolclass"] = poolclass

    if len(urls) == 1:
        db_url = urls[0]
        connect_args: dict[str, Any] = {}
        if db_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        engine = create_engine(db_url, connect_args=connect_args, **engine_kwargs)
    else:
        if not all(url.startswith("postgresql") for url in urls):
            raise ValueError("failover runtime DB URLs must all be PostgreSQL")
        engine = create_engine(
            "postgresql+psycopg://",
            creator=postgres_failover_creator(urls),
            **engine_kwargs,
        )

    if engine.dialect.name == "sqlite":
        _register_sqlite_pragmas(engine)
    _register_disconnect_handler(engine)
    return engine
