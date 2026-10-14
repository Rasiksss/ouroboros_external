"""The rekey's own contract: rows are keyed by ownership axes, and reads are bounded by them."""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from alembic import command
from sqlalchemy import inspect, text

from infra.migrations.runner import alembic_config, upgrade_head
from new_core.identity import SurfaceId
from ouroboros.storage.chat_store import append_chat_message, list_owner_messages
from ouroboros.storage.message_field_crypto import decrypt_field
from ouroboros.storage.runtime_models import ContextSummary, TaskEpisode
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.background_memory_db import memory_database
from tests.helpers.conversations import make_sql_runtime_persistence, sql_conversation_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.scope import scope_for
from tests.helpers.shared import TEST_SCOPE, scope_with

_OTHER_OWNER = scope_with(TEST_SCOPE, owner_id="another-owner")
_OTHER_AGENT = scope_with(TEST_SCOPE, agent_id="second")
_CHAT_ONE = scope_with(TEST_SCOPE, chat_id="chat-one")
_CHAT_TWO = scope_with(TEST_SCOPE, chat_id="chat-two")


@pytest.fixture
def summaries():
    """The committed summary's compare-and-swap is the background-memory repository's now."""
    yield from memory_database()


def _summary_axes(scope) -> dict[str, str]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "chat_id": scope.chat_id}


def _commit(database, scope, body: str, *, cursor: str = "") -> bool:
    summary = ContextSummary(**_summary_axes(scope), source_offsets_ref=cursor, summary_ref=body)
    return database.run(database.summaries.commit(summary, expected_cursor=""))


def _write(drive_root: pathlib.Path, scope, text_value: str, *, client_message_id: str) -> None:
    append_chat_message(
        drive_root,
        scope=scope,
        actor_type="owner",
        chat_id=scope.chat_id,
        user_id=1,
        text=text_value,
        source="browser",
        client_message_id=client_message_id,
    )


# --- ownership on the write side --------------------------------------------


def test_a_write_takes_its_axes_from_the_scope_and_from_no_other_parameter(tmp_path):
    """``chat_id`` and ``source`` are next to the scope and are not axes (D8, and the
    ``skill-bridge``/``internal`` trap)."""
    runtime_persistence.initialize()
    scope = scope_for("skill-bridge", "7")

    append_chat_message(
        tmp_path,
        scope=scope,
        actor_type="agent",
        chat_id=4242,
        user_id=1,
        text="from a skill",
        # The normalized task source for this surface is ``internal``; the axis is not.
        source="skill-bridge",
    )

    stored = list_owner_messages(tmp_path, scope=scope)[0]
    assert (stored.owner_id, stored.agent_id) == (scope.owner_id, scope.agent_id)
    assert stored.source == "internal"
    assert stored.metadata["chat_id"] == 4242


def test_a_skill_bridge_episode_stores_the_surface_not_the_task_source(tmp_path):
    from ouroboros.storage.task_episodes import store_task_episode

    scope = scope_for("skill-bridge", "7")

    episode = store_task_episode(
        {"id": "task-1", "text": "сделай", "source": "internal"},
        "готово",
        {"tool_calls": []},
        scope=scope,
        search_terms=[],
        execution_memory_repository=sql_execution_memory_repository(),
    )

    assert episode is not None
    assert episode.surface_id == SurfaceId.SKILL_BRIDGE.value == "skill-bridge"


def test_a_peer_id_never_reaches_an_ownership_column(tmp_path):
    """``state.owner_id`` on the dialogs path is a peer, not this instance's owner."""
    runtime_persistence.initialize()
    peer_scope = scope_for("dialogs", "42")

    append_chat_message(
        tmp_path,
        scope=peer_scope,
        actor_type="owner",
        chat_id=42,
        user_id="peer-7",
        text="привет",
        source="dialogs",
    )

    stored = list_owner_messages(tmp_path, scope=peer_scope)[0]
    assert stored.owner_id == TEST_SCOPE.owner_id
    assert stored.metadata["user_id"] == "peer-7"


# --- ownership on the read side ----------------------------------------------


def test_another_owners_message_is_not_read(tmp_path):
    runtime_persistence.initialize()
    _write(tmp_path, TEST_SCOPE, "mine", client_message_id="m-mine")
    _write(tmp_path, _OTHER_OWNER, "theirs", client_message_id="m-theirs")

    assert [m.content for m in list_owner_messages(tmp_path, scope=TEST_SCOPE)] == ["mine"]
    assert [m.content for m in list_owner_messages(tmp_path, scope=_OTHER_OWNER)] == ["theirs"]


def test_two_agents_of_one_owner_keep_separate_committed_summaries(tmp_path, summaries):
    """Under the constant ``summary_id`` the second agent's commit could never land."""
    store = runtime_persistence.initialize()

    first = _commit(summaries, TEST_SCOPE, "first agent")
    second = _commit(summaries, _OTHER_AGENT, "second agent")

    assert (first, second) == (True, True)
    assert sql_conversation_repository(store.engine).get_latest_context_summary(TEST_SCOPE).summary_ref == "first agent"
    assert (
        sql_conversation_repository(store.engine).get_latest_context_summary(_OTHER_AGENT).summary_ref == "second agent"
    )


def test_two_chats_of_one_owner_do_not_share_a_history_or_a_summary(tmp_path, summaries):
    """The exposure this change closes: on dialogs, peers differ only in the chat axis."""
    store = runtime_persistence.initialize()
    first, second = _CHAT_ONE, _CHAT_TWO
    _write(tmp_path, first, "first chat", client_message_id="m-1")
    _write(tmp_path, second, "second chat", client_message_id="m-2")

    committed = (_commit(summaries, first, "first summary"), _commit(summaries, second, "second summary"))

    assert committed == (True, True)
    assert [m.content for m in list_owner_messages(tmp_path, scope=first)] == ["first chat"]
    assert [m.content for m in list_owner_messages(tmp_path, scope=second)] == ["second chat"]
    assert sql_conversation_repository(store.engine).get_latest_context_summary(first).summary_ref == "first summary"
    assert sql_conversation_repository(store.engine).get_latest_context_summary(second).summary_ref == "second summary"


def test_a_second_chats_first_commit_creates_a_row_rather_than_conflicting_away(tmp_path, summaries):
    """At the pair the insert's ``DO NOTHING`` made the second chat permanently stale."""
    store = runtime_persistence.initialize()
    sql_conversation_repository(store.engine).upsert_context_summary(
        ContextSummary(**_summary_axes(_CHAT_ONE), summary_ref="sibling")
    )

    landed = _commit(summaries, _CHAT_TWO, "its own first")

    assert landed is True
    assert (
        sql_conversation_repository(store.engine).get_latest_context_summary(_CHAT_TWO).summary_ref == "its own first"
    )
    assert sql_conversation_repository(store.engine).get_latest_context_summary(_CHAT_ONE).summary_ref == "sibling"


def test_the_compare_and_swap_under_two_uncommitted_chats_writes_neither_over_the_other(tmp_path, summaries):
    """Both expect an empty cursor, so at the pair the ``WHERE`` matched a sibling's row."""
    store = runtime_persistence.initialize()

    for scope, body in ((_CHAT_ONE, "first"), (_CHAT_TWO, "second")):
        assert _commit(summaries, scope, body, cursor=f"cursor-{body}")

    with store.engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM context_summaries")).scalar_one()
    assert rows == 2
    assert sql_conversation_repository(store.engine).get_latest_context_summary(_CHAT_ONE).summary_ref == "first"
    assert sql_conversation_repository(store.engine).get_latest_context_summary(_CHAT_TWO).summary_ref == "second"


# --- the retired key ---------------------------------------------------------


def test_a_freshly_written_payload_carries_no_conversation_id(tmp_path):
    """``RuntimeModel`` allows extra fields, so removal is pinned by the stored bytes."""
    store = runtime_persistence.initialize()
    _write(tmp_path, TEST_SCOPE, "hello", client_message_id="m-1")
    sql_execution_memory_repository(store.engine).record_task_episode(
        TaskEpisode(**TEST_SCOPE.to_dict(), episode_id="e-1", task_id="task-1", user_text="q", answer_preview="a")
    )

    with store.engine.connect() as conn:
        payloads = [
            decrypt_field(row[0])
            for table in ("messages", "tasks", "task_episodes", "tool_invocations", "context_summaries")
            for row in conn.execute(text(f"SELECT payload_json FROM {table}")).fetchall()
        ]

    assert payloads
    assert all("conversation_id" not in json.loads(payload) for payload in payloads)


def test_no_production_module_imports_a_retired_symbol():
    retired = (
        "conversation_id_for_chat",
        "conversation_id_for_a2a",
        "resolve_conversation_id",
        "SHARED_CHAT_SUMMARY_ID",
        "SHARED_CHAT_SUMMARY_CONVERSATION_ID",
        "working_retrieval_key",
    )
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    roots = ("ouroboros", "supervisor", "transport", "background_memory", "new_core")
    offenders = [
        f"{path}: {name}"
        for root in roots
        for path in (repo_root / root).rglob("*.py")
        if "__pycache__" not in path.parts
        for name in retired
        if name in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


#: The statements that are owner-wide on purpose, named rather than left to look like misses:
#: durable-job admission serializes an owner, a job id is unique within an owner across their
#: agents, and the running-root ceiling `OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER` counts the
#: same way. Adding the agent to any of the three would contradict the rule it enforces.
_OWNER_WIDE_BY_DESIGN = ("pg_advisory_xact_lock", "FROM tasks WHERE owner_id=:owner_id AND")


def test_no_statement_binds_the_owner_without_the_agent():
    """The predicates are written out per statement; this is what keeps them complete."""
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    offenders = [
        f"{path.relative_to(repo_root)}:{node.lineno}"
        for path in (repo_root / "ouroboros" / "storage").rglob("*.py")
        if "migrations" not in path.parts and "__pycache__" not in path.parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ":owner_id" in node.value
        and ":agent_id" not in node.value
        and not any(marker in node.value for marker in _OWNER_WIDE_BY_DESIGN)
    ]

    assert offenders == []


def test_no_table_keeps_the_glued_key(tmp_path):
    store = runtime_persistence.initialize()
    inspector = inspect(store.engine)

    assert "conversations" not in inspector.get_table_names()
    for table in inspector.get_table_names():
        assert "conversation_id" not in {column["name"] for column in inspector.get_columns(table)}, table


# --- the migration -----------------------------------------------------------


def _upgrade_to(engine, revision: str) -> None:
    cfg = alembic_config(str(engine.url.render_as_string(hide_password=False)))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def _insert_legacy_episode(conn, episode_id: str, conversation_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO task_episodes (episode_id, task_id, conversation_id, created_at, payload_json) "
            "VALUES (:episode_id, :task_id, :conversation_id, '2026-01-01T00:00:00+00:00', :payload)"
        ),
        {
            "episode_id": episode_id,
            "task_id": f"task-{episode_id}",
            "conversation_id": conversation_id,
            "payload": json.dumps({"episode_id": episode_id, "conversation_id": conversation_id}),
        },
    )


def test_the_backfill_splits_a_known_surface_and_parks_the_rest(blank_schema):
    engine = blank_schema()
    _upgrade_to(engine, "0007")
    with engine.begin() as conn:
        _insert_legacy_episode(conn, "parseable", "dialogs:123")
        _insert_legacy_episode(conn, "legacy-spelling", "sberchat:5")
        _insert_legacy_episode(conn, "parked", "9f2c0a4b6e114f0c")

    upgrade_head(engine)

    with engine.connect() as conn:
        rows = {
            row._mapping["episode_id"]: (row._mapping["surface_id"], row._mapping["chat_id"])
            for row in conn.execute(text("SELECT episode_id, surface_id, chat_id FROM task_episodes"))
        }
    assert rows["parseable"] == ("dialogs", "123")
    assert rows["legacy-spelling"] == ("dialogs", "5")
    # Parked, not deleted: no minted scope can carry an empty chat, so it matches no read.
    assert rows["parked"][1] == ""

    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url="")
    for scope in (
        scope_for("dialogs", "123"),
        scope_for("skill-bridge", "whatever"),
    ):
        assert "parked" not in {
            episode.episode_id
            for episode in sql_execution_memory_repository(store.engine).list_task_episodes(scope=scope)
        }


def test_a_parked_invocation_is_skipped_rather_than_failing_the_read(blank_schema):
    """``list_tool_invocations`` is keyed by task alone, so no predicate can exclude a parked row.

    Its blank chat axis is one the model refuses, and the read runs while the live task writes
    its episode — the legacy row must not take the running task down with it.
    """
    engine = blank_schema()
    _upgrade_to(engine, "0007")
    with engine.begin() as conn:
        for invocation_id, conversation_id in (("usable", "dialogs:123"), ("parked", "9f2c0a4b6e114f0c")):
            conn.execute(
                text(
                    "INSERT INTO tool_invocations (tool_invocation_id, task_id, conversation_id, tool_name, "
                    "created_at, payload_json) VALUES (:invocation_id, 'task-1', :conversation_id, 'bash', "
                    "'2026-01-01T00:00:00+00:00', '{}')"
                ),
                {"invocation_id": invocation_id, "conversation_id": conversation_id},
            )

    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url="")
    listed = sql_execution_memory_repository(store.engine).list_tool_invocations(task_id="task-1")

    assert [invocation.tool_invocation_id for invocation in listed] == ["usable"]


def test_a_row_written_before_the_migration_reads_back_from_its_columns(blank_schema):
    """Its ``payload_json`` has no axes and does have the retired key (D12)."""
    engine = blank_schema()
    _upgrade_to(engine, "0007")
    with engine.begin() as conn:
        _insert_legacy_episode(conn, "legacy", "dialogs:123")

    upgrade_head(engine)
    store = make_sql_runtime_persistence(engine, db_url="")
    scope = scope_for("dialogs", "123")
    (episode,) = sql_execution_memory_repository(store.engine).list_task_episodes(scope=scope)

    assert (episode.surface_id, episode.chat_id) == ("dialogs", "123")
    assert (episode.owner_id, episode.agent_id) == (scope.owner_id, scope.agent_id)
    assert not hasattr(episode, "conversation_id")


def test_0012_downgrade_preserves_scoped_episode(blank_schema):
    from tests.helpers.migrations import downgrade_to, upgrade_to

    engine = blank_schema()
    upgrade_to(engine, "0012")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_episodes (episode_id, task_id, owner_id, agent_id, surface_id, chat_id, "
                "created_at, payload_json) VALUES ('e1', 't1', 'global', 'main', 'dialogs', '123', "
                "'2026-01-01T00:00:00+00:00', '{}')"
            )
        )

    downgrade_to(engine, "0007")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT conversation_id FROM task_episodes")).scalar_one() == "dialogs:123"
        assert "conversations" in inspect(engine).get_table_names()

    upgrade_to(engine, "0012")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0012"


def test_0014_downgrade_refusal_preserves_scoped_episode(blank_schema):
    from tests.helpers.migrations import downgrade_to, schema_snapshot

    engine = blank_schema()
    # Stopped at 0014 rather than at head: the revisions above come down cleanly, so a refusal
    # reached through them would be asserted against a schema their own downgrade had changed.
    _upgrade_to(engine, "0014")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_episodes (episode_id, task_id, owner_id, agent_id, surface_id, chat_id, "
                "created_at, payload_json) VALUES ('e1', 't1', 'global', 'main', 'dialogs', '123', "
                "'2026-01-01T00:00:00+00:00', '{}')"
            )
        )

    # 0014 is the floor, not the head; what sits above it comes down before it refuses.
    downgrade_to(engine, "0014")
    before = schema_snapshot(engine, data=True)
    with pytest.raises(RuntimeError, match="0014.*unsupported"):
        downgrade_to(engine, "0007")
    assert schema_snapshot(engine, data=True) == before


def test_the_preflight_refuses_a_schema_it_does_not_recognise(blank_schema):
    engine = blank_schema()
    _upgrade_to(engine, "0007")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE task_episodes"))

    with pytest.raises(Exception, match="0008 preflight"):
        upgrade_head(engine)
