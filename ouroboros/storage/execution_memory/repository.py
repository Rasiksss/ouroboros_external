import logging
import uuid
from collections.abc import Iterable
from contextlib import AbstractContextManager

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Row

from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.message_field_crypto import decrypt_field, encrypt_field
from ouroboros.storage.runtime_models import TaskEpisode, ToolInvocation
from ouroboros.storage.runtime_serialization import (
    dump_json as _dump_json,
    load_json as _load_json,
    load_json_list as _load_json_list,
)
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


class SqlExecutionMemoryRepository(IExecutionMemoryRepository):
    def __init__(self, transaction_manager: SqlTransactionManager) -> None:
        self.transaction_manager = transaction_manager

    @property
    def transaction(self) -> AbstractContextManager[None]:
        return self.transaction_manager.transaction

    def record_tool_invocation(self, invocation: ToolInvocation) -> ToolInvocation:
        """One statement: an append keyed by an id this caller minted."""
        invocation.tool_invocation_id = invocation.tool_invocation_id or uuid.uuid4().hex
        invocation.created_at = invocation.created_at or utc_now_iso()
        payload = invocation.model_dump(mode="json")
        params = {
            "tool_invocation_id": invocation.tool_invocation_id,
            "task_id": invocation.task_id,
            "owner_id": invocation.owner_id,
            "agent_id": invocation.agent_id,
            "surface_id": invocation.surface_id,
            "chat_id": invocation.chat_id,
            "tool_name": invocation.tool_name,
            "status": invocation.status,
            "args_redacted_json": encrypt_field(_dump_json(invocation.args_redacted)),
            "result_summary_json": encrypt_field(_dump_json(invocation.result_summary)),
            "result_index_json": _dump_json(invocation.result_index),
            "search_terms_json": _dump_json(invocation.search_terms),
            "payload_json": encrypt_field(_dump_json(payload)),
            "payload_hash": invocation.payload_hash,
            "sensitivity": invocation.sensitivity,
            "retention_class": invocation.retention_class,
            "created_at": invocation.created_at,
            "expires_at": invocation.expires_at,
        }
        with self.transaction, self.transaction_manager.connection() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO tool_invocations (
                        tool_invocation_id, task_id, owner_id, agent_id, surface_id, chat_id,
                        tool_name, status,
                        args_redacted_json, result_summary_json, result_index_json,
                        search_terms_json, payload_json, payload_hash, sensitivity,
                        retention_class, created_at, expires_at
                    ) VALUES (
                        :tool_invocation_id, :task_id, :owner_id, :agent_id, :surface_id, :chat_id,
                        :tool_name, :status,
                        :args_redacted_json, :result_summary_json, :result_index_json,
                        :search_terms_json, :payload_json, :payload_hash, :sensitivity,
                        :retention_class, :created_at, :expires_at
                    )
                    ON CONFLICT(tool_invocation_id) DO UPDATE SET
                        task_id=excluded.task_id,
                        owner_id=excluded.owner_id,
                        agent_id=excluded.agent_id,
                        surface_id=excluded.surface_id,
                        chat_id=excluded.chat_id,
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
        return invocation

    def list_tool_invocations(
        self,
        *,
        task_id: str,
        limit: int = 100,
    ) -> list[ToolInvocation]:
        # The task is the whole predicate, and it is required: no argument value may drop the
        # clause and merge other tasks' rows into the caller's episode.
        params: dict[str, str | int] = {"limit": max(1, int(limit or 1)), "task_id": str(task_id)}
        query = (
            "SELECT * FROM tool_invocations WHERE task_id = :task_id "
            "ORDER BY created_at DESC, tool_invocation_id DESC LIMIT :limit"
        )
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(text(query), params).fetchall()
        invocations: list[ToolInvocation] = []
        for row in rows:
            try:
                invocations.append(self._row_to_tool_invocation(row))
            except ValidationError:
                # Legacy rows without usable scope cannot contribute to an episode.
                log.warning("Skipped a tool_invocations row that carries no usable scope", exc_info=True)
        return invocations

    def _row_to_tool_invocation(self, row: Row[tuple[object, ...]]) -> ToolInvocation:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"]))
        payload.pop("conversation_id", None)
        payload.update({axis: mapping[axis] for axis in ("owner_id", "agent_id", "surface_id", "chat_id")})
        payload.update(
            {
                "tool_invocation_id": mapping["tool_invocation_id"],
                "task_id": mapping["task_id"],
                "tool_name": mapping["tool_name"],
                "status": mapping["status"],
                "args_redacted": _load_json(decrypt_field(mapping["args_redacted_json"])),
                "result_summary": _load_json(decrypt_field(mapping["result_summary_json"])),
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

    def record_task_episode(self, episode: TaskEpisode) -> TaskEpisode:
        episode.episode_id = episode.episode_id or uuid.uuid4().hex
        episode.created_at = episode.created_at or utc_now_iso()
        payload = episode.model_dump(mode="json")
        params = {
            "episode_id": episode.episode_id,
            "task_id": episode.task_id,
            "owner_id": episode.owner_id,
            "agent_id": episode.agent_id,
            "surface_id": episode.surface_id,
            "chat_id": episode.chat_id,
            "created_at": episode.created_at,
            "user_text": encrypt_field(episode.user_text),
            "answer_preview": encrypt_field(episode.answer_preview),
            "tools_used_json": _dump_json(episode.tools_used),
            "domains_json": _dump_json(episode.domains),
            "entities_json": encrypt_field(_dump_json(episode.entities)),
            "refs_json": encrypt_field(_dump_json(episode.refs)),
            "search_terms_json": _dump_json(episode.search_terms),
            "payload_json": encrypt_field(_dump_json(payload)),
            "expires_at": episode.expires_at,
        }
        columns = tuple(params)
        conflict = (
            "NOTHING"
            if episode.payload.get("logical_job")
            else "UPDATE SET "
            + ", ".join(f"{column}=excluded.{column}" for column in columns if column != "episode_id")
        )
        with self.transaction, self.transaction_manager.connection() as conn:
            conn.execute(
                text(
                    f"INSERT INTO task_episodes ({', '.join(columns)}) VALUES ({', '.join(':' + column for column in columns)}) "
                    f"ON CONFLICT(episode_id) DO {conflict}"
                ),
                params,
            )
            row = conn.execute(
                text("SELECT * FROM task_episodes WHERE episode_id=:id"), {"id": episode.episode_id}
            ).one()
        return self._row_to_task_episode(row)

    def list_task_episodes(
        self,
        *,
        scope: OwnerScope,
        domains: Iterable[str] | None = None,
        search_terms: Iterable[str] | None = None,
        limit: int = 100,
        snapshot: dict[str, str] | None = None,
    ) -> list[TaskEpisode]:
        clauses: list[str] = [
            "owner_id=:owner_id AND agent_id=:agent_id AND surface_id=:surface_id AND chat_id=:chat_id"
        ]
        params: dict[str, str | int] = {"limit": max(1, int(limit or 1)), **scope.to_dict()}
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
        if snapshot is not None:
            if not snapshot:
                return []
            keys = {f"episode_{i}": episode_id for i, episode_id in enumerate(snapshot)}
            clauses.append("episode_id IN (" + ", ".join(":" + key for key in keys) + ")")
            params.update(keys)
        where = " AND ".join(clauses)
        query = f"SELECT * FROM task_episodes WHERE {where} ORDER BY created_at DESC, episode_id DESC LIMIT :limit"
        with self.transaction_manager.connection() as conn:
            rows = conn.execute(text(query), params).fetchall()
        episodes = [self._row_to_task_episode(row) for row in rows]
        return [
            episode
            for episode in episodes
            if snapshot is None or snapshot.get(episode.episode_id) == episode.snapshot_version()
        ]

    @staticmethod
    def _append_json_search_clause(
        clauses: list[str],
        params: dict[str, str | int],
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

    def _row_to_task_episode(self, row: Row[tuple[object, ...]]) -> TaskEpisode:
        mapping = row._mapping
        payload = _load_json(decrypt_field(mapping["payload_json"]))
        payload.pop("conversation_id", None)
        payload.update({axis: mapping[axis] for axis in ("owner_id", "agent_id", "surface_id", "chat_id")})
        payload.update(
            {
                "episode_id": mapping["episode_id"],
                "task_id": mapping["task_id"],
                "created_at": mapping["created_at"],
                "user_text": decrypt_field(mapping["user_text"]),
                "answer_preview": decrypt_field(mapping["answer_preview"]),
                "tools_used": _load_json_list(mapping["tools_used_json"]),
                "domains": _load_json_list(mapping["domains_json"]),
                "entities": _load_json(decrypt_field(mapping["entities_json"])),
                "refs": _load_json(decrypt_field(mapping["refs_json"])),
                "search_terms": _load_json_list(mapping["search_terms_json"]),
                "expires_at": mapping["expires_at"],
            }
        )
        return TaskEpisode.model_validate(payload)
