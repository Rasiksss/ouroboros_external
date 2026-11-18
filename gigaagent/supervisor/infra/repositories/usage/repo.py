from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from infra.sql_connection import sql_connection
from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import LlmUsageRecord
from ouroboros.storage.runtime_serialization import dump_json
from ouroboros.utils import utc_now_iso
from supervisor.interfaces.usage import IUsageRepository, UsageStats


class SqlUsageRepository(IUsageRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_usage(self, usage: LlmUsageRecord) -> LlmUsageRecord:
        """One statement: an append. Nothing is read, so nothing can be lost."""
        if not usage.usage_id:
            usage.usage_id = uuid.uuid4().hex
        if not usage.created_at:
            usage.created_at = utc_now_iso()
        payload = usage.model_dump(mode="json")
        session_id = str(usage.metadata.get("session_id") or "")
        with sql_connection(self._engine) as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO llm_usage (
                        usage_id, owner_id, agent_id, task_id, session_id, provider, model,
                        prompt_tokens, completion_tokens, cached_tokens, created_at,
                        metadata_json, payload_json
                    ) VALUES (
                        :usage_id, :owner_id, :agent_id, :task_id, :session_id, :provider, :model,
                        :prompt_tokens, :completion_tokens, :cached_tokens, :created_at,
                        :metadata_json, :payload_json
                    )
                    """
                ),
                {
                    "usage_id": usage.usage_id,
                    "owner_id": usage.owner_id,
                    "agent_id": usage.agent_id,
                    "task_id": usage.task_id,
                    "session_id": session_id,
                    "provider": usage.provider,
                    "model": usage.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "cached_tokens": usage.cached_tokens,
                    "created_at": usage.created_at,
                    "metadata_json": dump_json(usage.metadata),
                    "payload_json": dump_json(payload),
                },
            )
            conn.commit()
        return usage

    def sum_usage_stats(self, *, session_id: str | None = None, scope: OwnerScope | None = None) -> UsageStats:

        clauses: list[str] = []
        params: dict[str, str] = {}
        if scope is not None:
            clauses.append("owner_id=:owner_id AND agent_id=:agent_id")
            params.update({"owner_id": scope.owner_id, "agent_id": scope.agent_id})
        if session_id:
            clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with sql_connection(self._engine) as conn:
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
