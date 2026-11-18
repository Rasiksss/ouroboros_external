from __future__ import annotations

from ouroboros.storage.runtime_models import ACTIVE_STATUSES

ACTIVE_STATUS_SQL = ",".join(f"'{status}'" for status in ACTIVE_STATUSES)

CURRENT_EXECUTION_SQL = (
    "status='running' AND NOT EXISTS (SELECT 1 FROM tasks AS newer WHERE newer.owner_id=tasks.owner_id "
    "AND newer.job_id=tasks.job_id AND newer.lineage_ordinal>tasks.lineage_ordinal)"
)


def lease_live_sql() -> str:
    from ouroboros.storage.task_results import HEARTBEAT_STALE_SEC

    return f"heartbeat_at::timestamptz > clock_timestamp() - INTERVAL '{HEARTBEAT_STALE_SEC} seconds'"


def execution_authority_sql() -> str:
    token = "metadata_json::jsonb->>'execution_claim'"
    return f"{CURRENT_EXECUTION_SQL} AND {lease_live_sql()} AND :claim_token<>'' AND {token}=:claim_token"


def insert_message_sql() -> str:
    return """
        INSERT INTO messages (
            message_id, dedup_key, owner_id, agent_id, chat_id, session_id, source, source_message_id,
            actor_type, channel, content, content_ref, content_hash, created_at,
            metadata_json, payload_json, chat_history_visible, inbound_unprocessed
        ) VALUES (
            :message_id, :dedup_key, :owner_id, :agent_id, :chat_id, :session_id, :source, :source_message_id,
            :actor_type, :channel, :content, :content_ref, :content_hash, :created_at,
            :metadata_json, :payload_json, :chat_history_visible, :inbound_unprocessed
        )
        ON CONFLICT (dedup_key) DO NOTHING
        """


def root_claim_sql() -> str:
    """Roots share the owner ceiling; ordinary roots also serialize within their chat."""
    foreground_free = """NOT EXISTS (
        SELECT 1 FROM tasks AS foreground
        WHERE foreground.owner_id=tasks.owner_id AND foreground.agent_id=tasks.agent_id
          AND foreground.surface_id=tasks.surface_id AND foreground.chat_id=tasks.chat_id
          AND foreground.task_type='task' AND foreground.parent_task_id=''
          AND NOT foreground.detached AND foreground.status='running'
    )"""
    below_ceiling = """(
        SELECT COUNT(*) FROM tasks AS running
        WHERE running.owner_id=tasks.owner_id AND running.task_type='task'
          AND running.parent_task_id='' AND running.status='running'
    ) < :max_running_root_jobs_per_owner"""
    return f"""(
        tasks.task_type <> 'task' OR COALESCE(tasks.parent_task_id, '') <> '' OR
        ({below_ceiling} AND (tasks.detached OR {foreground_free}))
    )"""


def claim_select_sql(extra_where: str = "") -> str:
    extra = f"\n            AND {extra_where}" if extra_where else ""
    one_run = root_claim_sql()
    return f"""
        SELECT * FROM tasks
        WHERE status IN ('queued', 'scheduled', 'interrupted')
        AND {one_run}
        {extra}
        ORDER BY priority ASC, created_at ASC, task_id ASC
        LIMIT 1
        """
