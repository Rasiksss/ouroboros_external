from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_models import TaskEpisode
from ouroboros.utils import (
    sanitize_tool_result_for_log,
    truncate_review_artifact,
    utc_now_iso,
)


def build_task_episode(
    task: dict[str, Any],
    response_text: str,
    llm_trace: dict[str, Any],
    *,
    scope: OwnerScope,
    search_terms: Iterable[str],
    created_at: str | None = None,
    retention_days: int = 90,
) -> TaskEpisode | None:
    task_id = str(task.get("job_id") or task.get("id") or task.get("task_id") or "").strip()
    user_text = str(task.get("text") or task.get("description") or "").strip()
    answer_preview = truncate_review_artifact(sanitize_tool_result_for_log(str(response_text or "")), limit=1000)
    calls = [call for call in (llm_trace.get("tool_calls") or []) if isinstance(call, dict)]
    tools_used = list(dict.fromkeys(str(call.get("tool") or "").strip() for call in calls if call.get("tool")))
    if not task_id or (not task.get("detached") and not any((user_text, answer_preview, tools_used))):
        return None
    invocation_ids = [str(call["tool_call_id"]) for call in calls if call.get("tool_call_id")]
    created = created_at or utc_now_iso()
    return TaskEpisode(
        episode_id=stable_retrieval_id("episode", scope.owner_id, scope.agent_id, task_id)
        if task.get("detached")
        else stable_retrieval_id("episode", task_id, user_text, answer_preview),
        task_id=task_id,
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        surface_id=str(scope.surface_id),
        chat_id=scope.chat_id,
        created_at=created,
        user_text=user_text,
        answer_preview=answer_preview,
        tools_used=tools_used,
        domains=list(dict.fromkeys(name.split(".", 1)[0] for name in tools_used)),
        refs={"tool_invocation_ids": invocation_ids} if invocation_ids else {},
        search_terms=list(dict.fromkeys(str(term) for term in search_terms if term))[:120],
        payload={"builder": "universal_v2", **({"logical_job": True} if task.get("detached") else {})},
        expires_at=retention_expiry(created, retention_days),
    )


def store_task_episode(
    task: dict[str, Any],
    response_text: str,
    llm_trace: dict[str, Any],
    *,
    scope: OwnerScope,
    execution_memory_repository: IExecutionMemoryRepository,
    search_terms: Iterable[str],
    retention_days: int = 90,
    created_at: str | None = None,
) -> TaskEpisode | None:
    episode = build_task_episode(
        task,
        response_text,
        llm_trace,
        scope=scope,
        search_terms=search_terms,
        retention_days=retention_days,
        created_at=created_at,
    )
    if episode is None:
        return None
    invocation_task_id = str(task.get("id") or task.get("task_id") or episode.task_id)
    invocations = execution_memory_repository.list_tool_invocations(task_id=invocation_task_id, limit=100)
    aliases: list[str] = []
    for invocation in invocations:
        result_index = invocation.result_index
        if not isinstance(result_index, Mapping):
            continue
        if isinstance(retrieval_aliases := result_index.get("retrieval_aliases"), list):
            aliases.extend(alias for alias in retrieval_aliases if isinstance(alias, str))
        for target, key in ((episode.entities, "entities"), (episode.refs, "refs")):
            if isinstance(values := result_index.get(key), Mapping):
                target.update({str(name): value for name, value in values.items() if str(name) not in target})
    indexed_terms = (term for invocation in invocations for term in invocation.search_terms)
    episode.search_terms = list(dict.fromkeys([*aliases, *episode.search_terms, *indexed_terms]))[:120]
    tool_ids = [invocation.tool_invocation_id for invocation in invocations if invocation.tool_invocation_id]
    if tool_ids:
        episode.refs["tool_invocation_ids"] = tool_ids
    return execution_memory_repository.record_task_episode(episode)


def retention_expiry(created_at: str, retention_days: int) -> str:
    parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    return (
        (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
        + timedelta(days=max(0, int(retention_days or 0)))
    ).isoformat()


def stable_retrieval_id(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:32]
