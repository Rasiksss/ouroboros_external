from __future__ import annotations

import argparse
import base64
import json
import uuid

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from infra.migrations import runner
from new_core.identity import OwnerScope
from ouroboros.storage.chat_store import build_chat_message
from ouroboros.storage.message_field_crypto import encrypt_field, reset_encryption_cache_for_tests
from ouroboros.storage.runtime_models import ContextSummary, TaskRequest
from ouroboros.storage.runtime_persistence import compose_runtime_persistence
from ouroboros.storage.task_episodes import build_task_episode
from tests.helpers.migrations import schema_snapshot, upgrade_to
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.scope import scope_for
from transport.a2a.executor import derive_a2a_chat_id


def _seed_legacy(
    store, *, task_id="task-1", context_id=" ctx-1", message_id="message-1", status="completed", detached=False
):
    scope = scope_for("a2a", derive_a2a_chat_id(task_id))
    message = build_chat_message(
        scope,
        actor_type="owner",
        chat_id=int(scope.chat_id),
        user_id=int(scope.chat_id),
        source="a2a",
        text=f"question for {task_id}",
        client_message_id=message_id,
        task_id=task_id,
        transport={"a2a_context_id": context_id} if context_id is not None else {},
        extra_metadata={"inbound_processed": False},
    )
    store.conversations_repo.append_message(message, scope=scope)
    old_job = uuid.uuid5(
        uuid.NAMESPACE_URL,
        json.dumps(
            [scope.owner_id, scope.agent_id, "a2a", scope.chat_id, scope.chat_id, "", message_id],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    ).hex
    task = store.tasks_repo.create_task(
        TaskRequest(
            id=old_job,
            source="a2a",
            detached=detached,
            scope=scope.to_dict(),
            correlation_id=task_id,
            text=message.content,
            through_message_id=message.message_id,
        ),
        status=status,
    )
    return scope, message, task


def _seed_summary(engine, scope, message):
    summary = ContextSummary(
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        chat_id=scope.chat_id,
        source_offsets_ref=message.message_id,
        summary_ref="Earlier facts about the employees.",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO context_summaries (summary_id, owner_id, agent_id, chat_id, created_at, "
                "source_offsets_ref, summary_ref, payload_json) "
                "VALUES (:summary_id, :owner_id, :agent_id, :chat_id, :created_at, :source_offsets_ref, :summary_ref, :payload)"
            ),
            {
                **summary.model_dump(),
                "summary_ref": encrypt_field(summary.summary_ref),
                "payload": encrypt_field(summary.model_dump_json()),
            },
        )
    return summary


@pytest.mark.parametrize("encrypted", [False, True])
def test_0033_merges_history_without_changing_message_or_task_ids(blank_schema, monkeypatch, request, encrypted):
    if encrypted:
        monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", base64.b64encode(b"a" * 32).decode())
        monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    reset_encryption_cache_for_tests()
    request.addfinalizer(reset_encryption_cache_for_tests)
    engine = blank_schema()
    upgrade_to(engine, "0022")
    store = compose_runtime_persistence(engine, db_urls=[])
    first_scope, first_message, first_task = _seed_legacy(store)
    _, second_message, second_task = _seed_legacy(store, task_id="task-2", message_id="message-2")
    reply = build_chat_message(
        first_scope,
        actor_type="agent",
        chat_id=first_scope.chat_id,
        user_id=0,
        source="a2a",
        text="previous answer",
        task_id=first_task.task_id,
    )
    store.conversations_repo.append_message(reply, scope=first_scope)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scratchpad_blocks (owner_id, agent_id, job_id, surface_id, chat_id, ts, content, status) "
                "VALUES (:owner, :agent, :job, '', '', '2026-10-07', 'remember', 'active')"
            ),
            {"owner": first_scope.owner_id, "agent": first_scope.agent_id, "job": first_task.job_id},
        )

    upgrade_to(engine, "0033")

    scope = scope_for("a2a", derive_a2a_chat_id(" ctx-1"))
    history = store.conversations_repo.list_messages(scope=scope)
    assert {row.message_id for row in history} == {
        first_message.message_id,
        second_message.message_id,
        reply.message_id,
    }
    assert store.conversations_repo.list_messages(scope=first_scope) == []
    for original in (first_task, second_task):
        migrated = store.tasks_repo.get_task(original.task_id)
        assert OwnerScope.from_dict(migrated.input["scope"]) == scope
        assert migrated.input["through_message_id"] == original.input["through_message_id"]
        assert migrated.input["transport"]["correlation_id"] == original.correlation_id
        assert migrated.job_id != original.job_id
        assert store.tasks_repo.get_owner_job(migrated.job_id, owner_id=scope.owner_id).task_id == original.task_id
    with engine.connect() as conn:
        job_id = conn.execute(text("SELECT job_id FROM scratchpad_blocks")).scalar_one()
        assert job_id == store.tasks_repo.get_task(first_task.task_id).job_id
        payload = conn.execute(
            text("SELECT metadata_json FROM messages WHERE message_id=:id"), {"id": first_message.message_id}
        ).scalar_one()
        assert payload.startswith("enc:v1:") is encrypted


@pytest.mark.parametrize("shared_context", [True, False])
def test_0033_keeps_summaries_with_the_history_they_cover(blank_schema, shared_context):
    engine = blank_schema()
    upgrade_to(engine, "0022")
    store = compose_runtime_persistence(engine, db_urls=[])
    scope, message, _ = _seed_legacy(store)
    summary = _seed_summary(engine, scope, message)
    later_scope, later, _ = _seed_legacy(
        store, task_id="task-2", message_id="message-2", context_id=" ctx-1" if shared_context else "task-1"
    )
    if not shared_context:
        _seed_summary(engine, later_scope, later)

    upgrade_to(engine, "0033")

    current = scope_for("a2a", derive_a2a_chat_id(" ctx-1"))
    migrated = store.conversations_repo.get_latest_context_summary(current)
    assert migrated.summary_ref == summary.summary_ref
    assert migrated.source_offsets_ref == message.message_id
    assert [
        row.message_id
        for row in store.conversations_repo.list_messages(scope=current, after_message_id=migrated.source_offsets_ref)
    ] == ([later.message_id] if shared_context else [])
    if not shared_context:
        assert store.conversations_repo.get_latest_context_summary(scope).source_offsets_ref == later.message_id


def test_0033_retains_the_successful_episode_of_a_retried_job(blank_schema):
    engine = blank_schema()
    upgrade_to(engine, "0022")
    store = compose_runtime_persistence(engine, db_urls=[])
    scope, _, failed = _seed_legacy(store, status="failed", detached=True)
    completed = failed.model_copy(
        update={
            "task_id": "attempt-2",
            "lineage_ordinal": 1,
            "status": "completed",
            "terminal_published_at": failed.created_at,
            "metadata": {"terminal_memory_done": True},
            "input": {**failed.input, "id": "attempt-2"},
        }
    )
    store.tasks_repo.insert_task(completed)
    episode = build_task_episode(completed.to_agent_input(), "Employee grades", {}, scope=scope, search_terms=[])
    store.memory_repo.record_task_episode(episode)

    upgrade_to(engine, "0033")

    current = scope_for("a2a", derive_a2a_chat_id(" ctx-1"))
    (saved,) = store.memory_repo.list_task_episodes(scope=current)
    assert saved.episode_id == episode.episode_id
    assert store.tasks_repo.get_job_or_task_statuses([saved.task_id], scope=current) == {saved.task_id: "completed"}
    assert store.tasks_repo.get_owner_job(saved.task_id, owner_id=current.owner_id).task_id == "attempt-2"


def test_0033_binary_rollback_requires_the_pre_upgrade_backup(blank_schema, monkeypatch):
    engine = blank_schema()
    upgrade_to(engine, "0022")
    _seed_legacy(compose_runtime_persistence(engine, db_urls=[]))
    cfg = runner.alembic_config(str(engine.url.render_as_string(hide_password=False)))
    cfg.cmd_opts = argparse.Namespace(x=["binary_rollback"])
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        command.current(cfg)

    upgrade_to(engine, "0033")
    before = schema_snapshot(engine, data=True)
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="binary rollback.*backup"):
            command.current(cfg)
    monkeypatch.setattr(runner, "head_schema_version", lambda: 22)
    with pytest.raises(RuntimeError, match="binary rollback.*backup"):
        runner.assert_schema_at_head(engine)
    assert schema_snapshot(engine, data=True) == before


def test_0033_batches_database_work_across_contexts(blank_schema):
    engine = blank_schema()
    upgrade_to(engine, "0032")
    store = compose_runtime_persistence(engine, db_urls=[])
    for number in range(24):
        scope, message, _ = _seed_legacy(
            store, task_id=f"task-{number}", context_id=f"ctx-{number}", message_id=f"message-{number}"
        )
        _seed_summary(engine, scope, message)
    calls = []

    def track_calls(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"SELECT", "WITH", "UPDATE", "INSERT"}:
            calls.append(statement)

    event.listen(engine, "before_cursor_execute", track_calls)
    try:
        upgrade_to(engine, "0033")
    finally:
        event.remove(engine, "before_cursor_execute", track_calls)

    assert len(calls) <= 40, f"24 contexts required {len(calls)} database calls"
    for number in range(24):
        current = scope_for("a2a", derive_a2a_chat_id(f"ctx-{number}"))
        (message,) = store.conversations_repo.list_messages(scope=current)
        assert store.conversations_repo.get_latest_context_summary(current).source_offsets_ref == message.message_id


@parametrize_from_dict(
    {
        "missing context": {"failure": "context"},
        "queued task": {"failure": "queued"},
        "running task": {"failure": "running"},
        "duplicate message ids": {"failure": "duplicate"},
        "mixed old and new identities": {"failure": "mixed"},
        "history before another chat's summary cursor": {"failure": "summary"},
    }
)
def test_0033_refuses_unsafe_migration_without_partial_writes(blank_schema, failure):
    engine = blank_schema()
    upgrade_to(engine, "0022")
    store = compose_runtime_persistence(engine, db_urls=[])
    scope, _, _ = _seed_legacy(
        store,
        context_id=None if failure == "context" else "ctx-1",
        status=failure if failure in ("queued", "running") else "completed",
    )
    if failure == "duplicate":
        _seed_legacy(store, task_id="task-2", context_id="ctx-1")
    elif failure == "mixed":
        current = build_chat_message(
            scope,
            actor_type="owner",
            chat_id=scope.chat_id,
            user_id=scope.chat_id,
            source="a2a",
            text="another context",
            client_message_id="current-message",
            task_id="current-task",
            transport={"a2a_context_id": "task-1", "correlation_id": "current-task"},
        )
        store.conversations_repo.append_message(current, scope=scope)
    elif failure == "summary":
        later_scope, later, _ = _seed_legacy(store, task_id="task-2", context_id="ctx-1", message_id="message-2")
        _seed_summary(engine, later_scope, later)
    before = schema_snapshot(engine, data=True)

    with pytest.raises(IntegrityError if failure == "duplicate" else RuntimeError):
        upgrade_to(engine, "0033")

    assert schema_snapshot(engine, data=True) == before
