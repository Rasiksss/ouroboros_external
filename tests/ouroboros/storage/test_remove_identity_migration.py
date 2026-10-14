"""Fresh and upgraded databases retire only the durable self-description."""

from alembic import command
from sqlalchemy import text

from infra.migrations.runner import alembic_config


def _config_for(engine):
    return alembic_config(str(engine.url.render_as_string(hide_password=False)))


def test_identity_removal_preserves_every_other_memory_and_cannot_restore_data(blank_schema):
    engine = blank_schema()
    cfg = _config_for(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0019")
        for owner, agent, name, kind, status in (
            ("one", "main", "identity", "identity", "active"),
            ("one", "other", "renamed", "identity", "archived"),
            ("two", "main", "identity", "project", "active"),
            ("two", "other", "lesson", "knowledge", "archived"),
            ("one", "main", "person", "user", "active"),
            ("one", "main", "style", "feedback", "active"),
            ("one", "main", "source", "reference", "active"),
        ):
            conn.execute(
                text(
                    "INSERT INTO memory (owner_id, agent_id, name, kind, description, content, status, "
                    "source, first_recorded_at, updated_at, last_recalled_at, recall_count) "
                    "VALUES (:owner, :agent, :name, :kind, 'identity reference', 'unchanged identity text', "
                    ":status, 'distilled', 11, 22, 33, 4)"
                ),
                {"owner": owner, "agent": agent, "name": name, "kind": kind, "status": status},
            )
        preserved = conn.execute(text("SELECT * FROM memory WHERE kind != 'identity' ORDER BY id")).mappings().all()

        command.upgrade(cfg, "0020")

        assert conn.execute(text("SELECT * FROM memory ORDER BY id")).mappings().all() == preserved
        command.downgrade(cfg, "0019")
        assert conn.execute(text("SELECT * FROM memory ORDER BY id")).mappings().all() == preserved
        command.upgrade(cfg, "head")
        current = conn.execute(text("SELECT * FROM memory ORDER BY id")).mappings().all()
        assert [{key: row[key] for key in preserved[0]} for row in current] == preserved


def test_fresh_upgrade_leaves_memory_empty(blank_schema):
    engine = blank_schema()
    cfg = _config_for(engine)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        assert conn.execute(text("SELECT * FROM memory")).all() == []
