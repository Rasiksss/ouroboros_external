from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE skills (
            skill_id text PRIMARY KEY, source_id text NOT NULL, external_id text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}', UNIQUE(source_id, external_id)
        );
        CREATE TABLE skill_artifact_objects (
            digest text PRIMARY KEY, object_key text NOT NULL UNIQUE,
            state text NOT NULL CHECK(state IN ('pending','ready','deleting')),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE TABLE skill_revisions (
            revision_id text PRIMARY KEY, skill_id text NOT NULL REFERENCES skills(skill_id),
            upstream_revision text NOT NULL, fingerprint text NOT NULL, digest text NOT NULL REFERENCES skill_artifact_objects(digest),
            normalizer_version integer NOT NULL, validation_version integer NOT NULL,
            kind text NOT NULL CHECK (kind IN ('instruction','executable')),
            record jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE(skill_id, fingerprint), UNIQUE(skill_id, revision_id),
            UNIQUE(skill_id, upstream_revision, normalizer_version, validation_version)
        );
        CREATE TABLE owner_skill_state (
            owner_id text NOT NULL, agent_id text NOT NULL, state_version bigint NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id, agent_id)
        );
        CREATE TABLE owner_skill_bindings (
            owner_id text NOT NULL, agent_id text NOT NULL, skill_id text NOT NULL,
            revision_id text NOT NULL,
            PRIMARY KEY(owner_id, agent_id, skill_id),
            FOREIGN KEY(owner_id, agent_id) REFERENCES owner_skill_state(owner_id, agent_id),
            FOREIGN KEY(skill_id, revision_id) REFERENCES skill_revisions(skill_id, revision_id)
        );
        CREATE TABLE skill_registry_policy (
            keyspace text PRIMARY KEY, digest text NOT NULL
        );
        CREATE INDEX idx_owner_skill_bindings_revision ON owner_skill_bindings(revision_id);
        CREATE TABLE skill_command_receipts (
            owner_id text NOT NULL, agent_id text NOT NULL, idempotency_key text NOT NULL,
            request_hash text NOT NULL, result jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(owner_id,agent_id,idempotency_key),
            FOREIGN KEY(owner_id,agent_id) REFERENCES owner_skill_state(owner_id,agent_id)
        );
        CREATE TABLE skill_binding_snapshots (
            snapshot_key text PRIMARY KEY, record jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX idx_skill_binding_snapshots_created ON skill_binding_snapshots(created_at);
        CREATE TABLE task_skill_bindings (
            task_id text PRIMARY KEY, owner_id text NOT NULL, agent_id text NOT NULL,
            scope jsonb NOT NULL,
            snapshot_key text NOT NULL REFERENCES skill_binding_snapshots(snapshot_key),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE TABLE task_skill_binding_revisions (
            task_id text NOT NULL REFERENCES task_skill_bindings(task_id) ON DELETE CASCADE,
            skill_id text NOT NULL, revision_id text NOT NULL,
            PRIMARY KEY(task_id, skill_id),
            FOREIGN KEY(skill_id, revision_id) REFERENCES skill_revisions(skill_id, revision_id)
        );
        CREATE INDEX idx_task_skill_bindings_created ON task_skill_bindings(created_at);
        CREATE INDEX idx_task_skill_bindings_snapshot ON task_skill_bindings(snapshot_key);
        CREATE INDEX idx_task_skill_binding_revisions_revision ON task_skill_binding_revisions(revision_id);
        CREATE INDEX idx_skill_receipts_created ON skill_command_receipts(created_at);
        CREATE INDEX idx_skill_receipts_revision ON skill_command_receipts((result->>'installed_revision'));
        CREATE INDEX idx_skill_artifacts_created ON skill_artifact_objects(created_at);
        CREATE INDEX idx_skill_revisions_created ON skill_revisions(created_at);
        CREATE INDEX idx_skill_revisions_digest ON skill_revisions(digest);
        CREATE TABLE skill_catalogue_snapshots (
            snapshot_id text PRIMARY KEY, query_hash text NOT NULL,
            total integer NOT NULL, byte_size integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            expires_at timestamptz NOT NULL, fetched_at timestamptz
        );
        CREATE INDEX idx_skill_catalogue_query ON skill_catalogue_snapshots(query_hash, created_at DESC);
        CREATE INDEX idx_skill_catalogue_expiry ON skill_catalogue_snapshots(expires_at);
        CREATE TABLE skill_catalogue_cards (
            snapshot_id text NOT NULL REFERENCES skill_catalogue_snapshots(snapshot_id) ON DELETE CASCADE,
            position integer NOT NULL, card jsonb NOT NULL,
            PRIMARY KEY(snapshot_id, position)
        );
        CREATE TABLE skill_catalogue_loads (
            query_hash text PRIMARY KEY, token text NOT NULL, expires_at timestamptz NOT NULL
        );
        CREATE INDEX idx_skill_catalogue_loads_expiry ON skill_catalogue_loads(expires_at);
    """)


def downgrade() -> None:
    for table in (
        "skill_catalogue_loads",
        "skill_catalogue_cards",
        "skill_catalogue_snapshots",
        "task_skill_binding_revisions",
        "task_skill_bindings",
        "skill_binding_snapshots",
        "skill_command_receipts",
        "owner_skill_bindings",
        "owner_skill_state",
        "skill_registry_policy",
        "skill_revisions",
        "skill_artifact_objects",
        "skills",
    ):
        op.execute(f"DROP TABLE {table}")
