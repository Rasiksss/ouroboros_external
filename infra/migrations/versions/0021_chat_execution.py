"""Chat-scoped foreground execution and scratchpad; requires stopped writers."""

from __future__ import annotations

import json
import logging
from typing import Any, TypedDict

import sqlalchemy as sa
from alembic import op
from cryptography.exceptions import InvalidTag

from new_core.identity import SURFACES_WITH_A_PERSON, OwnerScope, SurfaceId
from ouroboros.storage.message_field_crypto import FieldKeyUnavailable, decrypt_field

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

log = logging.getLogger(__name__)


class _TaskChat(TypedDict):
    task_id: str
    surface_id: str
    chat_id: str


def _internal_chat(chat: str) -> bool:
    try:
        return int(chat) < 0
    except ValueError:
        return False


def _object(value: str, dialect: str) -> dict[str, Any]:
    decoded = json.loads(decrypt_field(value, dialect_name=dialect) or "{}")
    if not isinstance(decoded, dict):
        raise ValueError("stored identity is not an object")
    return decoded


def _preflight() -> tuple[list[_TaskChat], list[OwnerScope]]:
    bind = op.get_bind()
    owners = "SELECT DISTINCT owner_id, agent_id FROM scratchpad_blocks WHERE job_id=''"
    shared: dict[tuple[str, str], set[tuple[SurfaceId, str] | None]] = {
        (row.owner_id, row.agent_id): set() for row in bind.execute(sa.text(owners))
    }

    def remember(pair: tuple[str, str], source: str, chat: str) -> None:
        surface = SurfaceId.coerce(source)
        if pair not in shared or surface is None or surface not in SURFACES_WITH_A_PERSON:
            return
        chat = chat.strip()
        if not _internal_chat(chat):
            shared[pair].add((surface, chat) if chat else None)

    tasks: list[_TaskChat] = []
    for row in bind.execute(
        sa.text("SELECT task_id, owner_id, agent_id, source, chat_id, input_json, payload_json FROM tasks")
    ):
        pair = (row.owner_id, row.agent_id)
        try:
            inputs = _object(row.input_json, bind.dialect.name)
            payload = _object(row.payload_json, bind.dialect.name)
            saved = payload.get("input", inputs)
            if not isinstance(saved, dict):
                raise ValueError("stored task input is not an object")
            raw_scope = saved.get("scope") or inputs.get("scope")
            if raw_scope:
                scope = OwnerScope.from_dict(raw_scope)
                if (scope.owner_id, scope.agent_id) != pair:
                    raise ValueError("stored scope disagrees with ownership columns")
                surface, chat = str(scope.surface_id), scope.chat_id
            else:
                surface = str(SurfaceId.coerce(row.source) or "")
                chat = str(row.chat_id or "").strip()
        except (ValueError, TypeError, InvalidTag) as exc:
            raise RuntimeError(f"0021 preflight {pair!r}: unreadable task identity ({row.task_id})") from exc
        tasks.append({"task_id": row.task_id, "surface_id": surface, "chat_id": chat})
        remember(pair, surface, chat)
    for row in bind.execute(
        sa.text(
            "SELECT state.owner_id, state.agent_id, state.payload_json FROM owner_runtime_state AS state "
            f"JOIN ({owners}) AS shared USING (owner_id, agent_id)"
        )
    ):
        pair = (row.owner_id, row.agent_id)
        try:
            saved_scope = _object(row.payload_json, bind.dialect.name).get("owner_scope")
            if saved_scope is not None:
                scope = OwnerScope.from_dict(saved_scope)
                if (scope.owner_id, scope.agent_id) != pair:
                    raise ValueError("stored owner scope disagrees with ownership columns")
                remember(pair, str(scope.surface_id), scope.chat_id)
        except FieldKeyUnavailable:
            raise
        except (ValueError, TypeError, InvalidTag):
            shared[pair].add(None)
    for row in bind.execute(
        sa.text(
            "SELECT history.* FROM ("
            "SELECT owner_id, agent_id, source, chat_id FROM messages UNION "
            "SELECT owner_id, agent_id, :browser, chat_id FROM chats"
            f") AS history JOIN ({owners}) AS shared USING (owner_id, agent_id)"
        ),
        {"browser": str(SurfaceId.BROWSER)},
    ):
        remember((row.owner_id, row.agent_id), row.source, row.chat_id)
    partitions = []
    for (owner, agent), candidates in shared.items():
        if len(candidates) == 1 and (candidate := next(iter(candidates))) is not None:
            partitions.append(OwnerScope(owner_id=owner, agent_id=agent, surface_id=candidate[0], chat_id=candidate[1]))
    return tasks, partitions


def upgrade() -> None:
    tasks, partitions = _preflight()
    bind = op.get_bind()
    op.add_column("tasks", sa.Column("surface_id", sa.Text(), nullable=False, server_default=""))
    for column in ("surface_id", "chat_id"):
        op.add_column("scratchpad_blocks", sa.Column(column, sa.Text(), nullable=False, server_default=""))
    if tasks:
        bind.execute(sa.text("UPDATE tasks SET surface_id=:surface_id, chat_id=:chat_id WHERE task_id=:task_id"), tasks)
    if partitions:
        bind.execute(
            sa.text(
                "UPDATE scratchpad_blocks SET surface_id=:surface_id, chat_id=:chat_id "
                "WHERE owner_id=:owner_id AND agent_id=:agent_id AND job_id=''"
            ),
            [scope.to_dict() for scope in partitions],
        )
    removed = bind.execute(sa.text("DELETE FROM scratchpad_blocks WHERE job_id='' AND chat_id=''"))
    if removed.rowcount:
        log.warning("0021 discarded %d ambiguous legacy foreground scratchpad block(s)", removed.rowcount)
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("chat_id", existing_type=sa.Text(), nullable=False, server_default="")
    op.drop_index("uq_tasks_one_running_foreground_root_per_owner_agent", table_name="tasks")
    predicate = sa.text("status = 'running' AND parent_task_id = '' AND task_type = 'task' AND NOT detached")
    op.create_index(
        "uq_tasks_one_running_foreground_root_per_chat",
        "tasks",
        ["owner_id", "agent_id", "surface_id", "chat_id"],
        unique=True,
        sqlite_where=predicate,
        postgresql_where=predicate,
    )
    op.drop_index("idx_scratchpad_blocks_owner_job_status_id", table_name="scratchpad_blocks")
    op.create_index(
        "idx_scratchpad_blocks_partition_status_id",
        "scratchpad_blocks",
        ["owner_id", "agent_id", "job_id", "surface_id", "chat_id", "status", "id"],
    )


def downgrade() -> None:
    raise RuntimeError("0021 requires a coordinated backup restore to downgrade; chat partitions cannot be merged")
