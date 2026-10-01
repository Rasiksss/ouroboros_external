"""Dialect-specific SQL fragments for the runtime metadata store."""

from __future__ import annotations


def is_postgres(dialect_name: str) -> bool:
    return dialect_name == "postgresql"


def insert_conversation_sql(dialect_name: str) -> str:
    if is_postgres(dialect_name):
        return """
            INSERT INTO conversations (
                conversation_id, session_id, source, created_at, updated_at
            ) VALUES (
                :conversation_id, :session_id, :source, :created_at, :updated_at
            )
            ON CONFLICT (conversation_id) DO NOTHING
            """
    return """
        INSERT OR IGNORE INTO conversations (
            conversation_id, session_id, source, created_at, updated_at
        ) VALUES (
            :conversation_id, :session_id, :source, :created_at, :updated_at
        )
        """


def insert_message_sql(dialect_name: str) -> str:
    if is_postgres(dialect_name):
        return """
            INSERT INTO messages (
                message_id, conversation_id, session_id, source, source_message_id,
                actor_type, channel, content, content_ref, content_hash, created_at,
                metadata_json, payload_json, chat_history_visible
            ) VALUES (
                :message_id, :conversation_id, :session_id, :source, :source_message_id,
                :actor_type, :channel, :content, :content_ref, :content_hash, :created_at,
                :metadata_json, :payload_json, :chat_history_visible
            )
            ON CONFLICT (message_id) DO NOTHING
            """
    return """
        INSERT OR IGNORE INTO messages (
            message_id, conversation_id, session_id, source, source_message_id,
            actor_type, channel, content, content_ref, content_hash, created_at,
            metadata_json, payload_json, chat_history_visible
        ) VALUES (
            :message_id, :conversation_id, :session_id, :source, :source_message_id,
            :actor_type, :channel, :content, :content_ref, :content_hash, :created_at,
            :metadata_json, :payload_json, :chat_history_visible
        )
        """


def claim_select_sql(dialect_name: str, extra_where: str = "") -> str:
    extra = f"\n            AND {extra_where}" if extra_where else ""
    if is_postgres(dialect_name):
        return f"""
            SELECT * FROM tasks
            WHERE status IN ('queued', 'scheduled', 'interrupted')
            {extra}
            ORDER BY priority ASC, created_at ASC, task_id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
    return f"""
        SELECT * FROM tasks
        WHERE status IN ('queued', 'scheduled', 'interrupted')
        {extra}
        ORDER BY priority ASC, created_at ASC, task_id ASC
        LIMIT 1
        """
