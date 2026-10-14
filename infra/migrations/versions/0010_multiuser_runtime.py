"""What one deployment's several instances need from the runtime database.

One revision rather than the seven this work was written as. The seven were never applied
outside a development checkout, so no database has a stamp between ``0009`` and this one,
and splitting history no deployment lived through buys nothing. They are kept below as
named steps in their original order, because the order is load-bearing: parentage reads the
run-addressing columns it hands back, and the message lift runs before its own DDL.

Seven concerns, each a step:

* **Run addressing.** A running task names the instance that holds it and can be told to
  stop. Two nullable columns on ``tasks`` and one index for the stale-lease sweep, which
  reads ``(status, heartbeat_at)``. No backfill: a row written before the upgrade has no
  instance and no stop, which reads correctly as "claimed by nobody we can name" — and the
  sweep skips it, so an old instance that is not writing leases cannot have its live runs
  reclaimed.
* **Owner runtime state, spend and audit.** ``owner_runtime_state`` was one row keyed by
  ``tenant_id`` — a value that names a deployment, not a person, and is the same for
  everyone the instance serves. It is rekeyed onto the ownership pair, matching every other
  table ``0008`` moved. ``llm_usage`` and ``audit_events`` had no ownership axes at all, so
  spend could not be attributed to a person and an audit trail could not answer whose action
  it recorded; they take ``owner_id`` and ``agent_id`` as columns, filled at write time from
  the scope of the work — never derived later by joining through ``task_id``, because work
  outside a task has no task to join through and a deleted task takes its rows' attribution
  with it. Existing rows are backfilled with the legacy owner rather than left null: a null
  would have to be handled by every reader forever, and the legacy value is the truthful
  answer for rows written when there was one owner.
* **Task parentage.** The one-run-per-owner rule counts an owner's *root* runs: a subtask
  inherits its parent's scope, so counting records rather than roots would stop a parent
  from starting the child it then waits for. The claim decides that inside its own
  statement, which means it needs the parentage in SQL rather than inside ``payload_json``.
  Existing non-terminal rows are backfilled from their payload — leaving them empty would
  make every queued subtask look like a root, and a parent waiting on a child that its own
  run blocks does not recover on its own.
* **Message identity.** A message's identity was six values and none of them the owner. A
  chat id is unique within an owner and not across owners, so two owners fronted by one
  proxy collide on it: the second owner's insert is refused by the primary key, and its
  submission can acknowledge the first owner's row. The identity moves off the primary key
  onto ``dedup_key``, which folds ``owner_id`` and ``agent_id`` over the old value. Off the
  primary key, because a row's ``payload_json`` carries its own ``message_id`` and is
  encrypted: a rewritten key column would read back as the old value, and this revision has
  no key with which to re-encrypt.
* **Deployment keyspace.** A per-pod tenant id or prefix gives that pod a private keyspace
  inside a correctly configured shared bucket, and nothing in the process can see its peers'
  environment. The resolved triple — backend, bucket, key prefix — is recorded once, here
  rather than in object storage: a marker inside the prefix under test is written into the
  divergent instance's own private keyspace and never meets the other one, and a marker
  outside the prefix breaks the reason the prefix exists.
* **Soft-timeout notice.** The sweep runs on every supervisor instance, so several of them
  list one overdue record before any of them writes. One nullable column carries the claim,
  taken by a statement whose predicate is ``soft_notice_at IS NULL``, so exactly one caller
  may announce. A flag merged into ``metadata_json`` could not refuse the second announcer:
  that merge is a read-modify-write in Python and is last-writer-wins on the column.
* **Consolidation leases.** Consolidation's single-flight rule and its consecutive-failure
  cooldown were two Python dicts on the handler, justified by the handler being one
  long-lived object on one memory thread. That premise is per-process. A table of its own
  rather than a column on an existing row: ``owner_runtime_state`` is rewritten whole by the
  supervisor, which would erase a lease as a side effect of unrelated work, and
  ``context_summaries`` does not exist yet when the lease has to be taken.

**Not safe for a rolling deploy.** Across the rekey an old instance writes the tenant-keyed
row while a new one writes the owner-keyed one; an old instance claims without the root
predicate and a new one with it; an old instance inserts messages with the old conflict
target. Apply this with runtimes stopped — which is the sequence ``make-runtime-writes-
atomic`` already made explicit by taking migration out of the runtime. Only the lease table
would have been safe to roll through, and it does not travel alone.

Every check the steps share runs in ``_preflight`` before the first DDL: pysqlite does not
roll back DDL, and aborting mid-way leaves a schema no retry can get past.

Every literal is written out here rather than imported: a revision is history the moment it
is applied, so it carries its own copy of what it needs.
"""

from __future__ import annotations

import json
import logging
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

log = logging.getLogger("0010_multiuser_runtime")

_DEFAULT_OWNER_ID = "global"
_DEFAULT_AGENT_ID = "main"

_TASKS = "tasks"
_MESSAGES = "messages"
_STATE = "owner_runtime_state"
_STATE_OLD = "owner_runtime_state_pre_0010"
_USAGE = "llm_usage"
_AUDIT = "audit_events"
_KEYSPACE = "deployment_keyspace"
_LEASES = "consolidation_leases"

_SWEEP_INDEX = "idx_tasks_status_heartbeat"
_USAGE_OWNER_INDEX = "idx_llm_usage_owner"
_AUDIT_OWNER_INDEX = "idx_audit_events_owner"
_CLAIM_INDEX = "idx_tasks_owner_running_roots"
_ONE_ROOT_INDEX = "uq_tasks_one_running_root_per_owner"
_DEDUP_INDEX = "uq_messages_dedup_key"

_PARENT = "parent_task_id"
_DEDUP = "dedup_key"
#: Types whose records are not a user's work and never occupy an owner's one slot.
_EXEMPT_TASK_TYPES = ("memory_distillation", "context_consolidation", "a2a")
#: The statuses a run occupies while it holds its owner's one slot.
_ACTIVE = ("queued", "scheduled", "interrupted", "running")
#: The single ``deployment_keyspace`` row's key. A constant rather than a generated id:
#: "one row" is the rule, and a primary key that can hold only one value is how the
#: database states it.
_ONE_ROW = "keyspace"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _preflight() -> None:
    """Refuse a schema that is not the one ``0009`` leaves behind, and one this revision half-wrote.

    Every check the steps below need, run before any of them writes. A single revision has a
    single stamp, so a database it aborted part-way through cannot be resumed from where it
    stopped; the answer is that nothing between this function and the end can fail on a
    schema it accepted.
    """
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names())
    missing = [name for name in (_TASKS, _MESSAGES, _STATE, _USAGE, _AUDIT) if name not in present]
    if missing:
        raise RuntimeError(f"0010 preflight: expected tables missing from the schema 0009 leaves behind: {missing}")
    for table in (_KEYSPACE, _LEASES):
        if table in present:
            raise RuntimeError(f"0010 preflight: {table} already exists; this revision has partly run")

    task_columns = _columns(_TASKS)
    for column in ("runtime_id", "stop_requested_at", _PARENT, "soft_notice_at"):
        if column in task_columns:
            raise RuntimeError(f"0010 preflight: {_TASKS}.{column} already exists; this revision has partly run")

    if "tenant_id" not in _columns(_STATE):
        raise RuntimeError(f"0010 preflight: {_STATE}.tenant_id is absent; this is not an 0009 schema")
    for table in (_USAGE, _AUDIT):
        if "owner_id" in _columns(table):
            raise RuntimeError(f"0010 preflight: {table}.owner_id already exists; this revision has partly run")

    message_columns = _columns(_MESSAGES)
    if _DEDUP in message_columns:
        raise RuntimeError(f"0010 preflight: {_MESSAGES}.{_DEDUP} already exists; this revision has partly run")
    for required in ("message_id", "owner_id", "agent_id"):
        if required not in message_columns:
            raise RuntimeError(f"0010 preflight: {_MESSAGES}.{required} is missing; this is not an 0009 schema")

    # Every pre-rekey row lands on the same owner and agent, so more than one is a collision
    # the new key would refuse.
    rows = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {_STATE}")).scalar()
    if int(rows or 0) > 1:
        raise RuntimeError(
            f"0010 preflight: {_STATE} holds several tenant rows; they cannot be collapsed onto one owner "
            "without choosing which to lose"
        )


# --- Run addressing -----------------------------------------------------------


def _add_run_addressing() -> None:
    """Name the instance holding a run, and give a stop somewhere to be recorded."""
    op.add_column(_TASKS, sa.Column("runtime_id", sa.Text(), nullable=True))
    op.add_column(_TASKS, sa.Column("stop_requested_at", sa.Text(), nullable=True))
    op.create_index(_SWEEP_INDEX, _TASKS, ["status", "heartbeat_at"])


# --- Owner runtime state, spend and audit -------------------------------------


def _add_owner_axes(table: str) -> None:
    op.add_column(table, sa.Column("owner_id", sa.Text(), nullable=False, server_default=_DEFAULT_OWNER_ID))
    op.add_column(table, sa.Column("agent_id", sa.Text(), nullable=False, server_default=_DEFAULT_AGENT_ID))


def _rekey_owner_runtime_state() -> None:
    """Move the row from the tenant key onto the ownership pair, carrying its contents.

    Rebuilt rather than altered in place: the key itself changes, and a table rebuild is the
    one shape that means the same thing on both dialects.
    """
    op.rename_table(_STATE, _STATE_OLD)
    op.create_table(
        _STATE,
        sa.Column("owner_id", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Text(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {_STATE} (owner_id, agent_id, payload_json, updated_at) "
            f"SELECT :owner_id, :agent_id, payload_json, updated_at FROM {_STATE_OLD}"
        ),
        {"owner_id": _DEFAULT_OWNER_ID, "agent_id": _DEFAULT_AGENT_ID},
    )
    op.drop_table(_STATE_OLD)


def _attribute_spend_and_audit() -> None:
    """Give spend and the audit trail the owner they were recorded without."""
    _add_owner_axes(_USAGE)
    _add_owner_axes(_AUDIT)
    # What the per-owner reads walk: spend for one owner over a window, and that owner's
    # audit trail newest first.
    op.create_index(_USAGE_OWNER_INDEX, _USAGE, ["owner_id", "agent_id", "created_at"])
    op.create_index(_AUDIT_OWNER_INDEX, _AUDIT, ["owner_id", "agent_id", "timestamp"])


# --- Task parentage -----------------------------------------------------------


def _payload_parent(payload: dict) -> str:
    """The parent this payload names, wherever the writer of the row put it.

    A row written before this revision carries it inside ``input`` — the agent's task dict,
    which is where ``TaskRequest`` has always serialized it — and only a row written by an
    instance that already has the record field carries it at the top level. Reading one
    place backfilled nothing, so every existing subtask came out looking like a root.
    """
    top = str((payload or {}).get(_PARENT) or "").strip()
    if top:
        return top
    task_input = (payload or {}).get("input")
    if not isinstance(task_input, dict):
        return ""
    return str(task_input.get(_PARENT) or "").strip()


def _backfill_parents() -> None:
    """Copy the parent out of each non-terminal payload, row by row.

    Only non-terminal rows: a finished task's parentage is history, and the predicate that
    needs this column never looks at one.
    """
    bind = op.get_bind()
    placeholders = ", ".join(f":status_{index}" for index, _ in enumerate(_ACTIVE))
    params = {f"status_{index}": status for index, status in enumerate(_ACTIVE)}
    rows = bind.execute(
        sa.text(f"SELECT task_id, payload_json FROM {_TASKS} WHERE status IN ({placeholders})"),
        params,
    ).fetchall()
    for task_id, payload_text in rows:
        try:
            payload = json.loads(payload_text or "{}")
        except (TypeError, ValueError):
            continue
        parent = _payload_parent(payload if isinstance(payload, dict) else {})
        if not parent:
            continue
        bind.execute(
            sa.text(f"UPDATE {_TASKS} SET {_PARENT}=:parent WHERE task_id=:task_id"),
            {"parent": parent, "task_id": task_id},
        )


def _exempt_sql() -> str:
    return ", ".join(f"'{task_type}'" for task_type in _EXEMPT_TASK_TYPES)


def _free_duplicate_running_roots() -> None:
    """Leave one running root per owner; the rest go back to being claimable.

    A database carrying two of an owner's roots cannot take the unique index below, and
    that state is exactly what this revision exists to make impossible. The younger rows
    become ``interrupted`` rather than terminal: the status is claimable, so the work
    starts again rather than being lost, which is what a released run already does.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT task_id, owner_id, agent_id FROM {_TASKS}
            WHERE status = 'running' AND COALESCE({_PARENT}, '') = ''
              AND task_type NOT IN ({_exempt_sql()})
            ORDER BY owner_id, agent_id, started_at, task_id
            """
        )
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    for task_id, owner_id, agent_id in rows:
        key = (str(owner_id), str(agent_id))
        if key not in seen:
            seen.add(key)
            continue
        # The stop goes with the instance: a row demoted here is meant to be claimed again,
        # and one carrying a stop from the run it is no longer part of stops on its first
        # check instead of starting.
        bind.execute(
            sa.text(
                f"UPDATE {_TASKS} SET status='interrupted', runtime_id=NULL, stop_requested_at=NULL "
                "WHERE task_id=:task_id"
            ),
            {"task_id": task_id},
        )


def _add_task_parentage() -> None:
    op.add_column(_TASKS, sa.Column(_PARENT, sa.Text(), nullable=False, server_default=""))
    _backfill_parents()
    # What the claim's NOT EXISTS scans: one owner's active roots, and nothing else.
    op.create_index(_CLAIM_INDEX, _TASKS, ["owner_id", "agent_id", "status", _PARENT])
    # And what makes the rule hold when two claimants start from nothing at the same
    # instant: the predicate reads a snapshot, so neither sees the other's uncommitted
    # write. A unique index is the database refusing the second row rather than each
    # transaction deciding for itself.
    _free_duplicate_running_roots()
    op.create_index(
        _ONE_ROOT_INDEX,
        _TASKS,
        ["owner_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text(f"status = 'running' AND {_PARENT} = '' AND task_type NOT IN ({_exempt_sql()})"),
        sqlite_where=sa.text(f"status = 'running' AND {_PARENT} = '' AND task_type NOT IN ({_exempt_sql()})"),
    )


# --- Message identity ---------------------------------------------------------
#
# The ``dedup_key`` column is added with an empty default so the ``NOT NULL`` holds while
# existing rows are being lifted. It is deliberately not dropped afterwards: on SQLite that
# means a batch rebuild of ``messages``, which is a real risk taken against a hypothetical
# writer — the model requires the field, and the one insert statement always binds it.


def _dedup_key(owner_id: str, agent_id: str, inner: str) -> str:
    """The same fold the application computes, written out rather than imported."""
    material = json.dumps(
        [str(owner_id or ""), str(agent_id or ""), str(inner or "")],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, material).hex


def _lifted_identities() -> list[dict[str, str]]:
    """The identity every existing row would have been written with, computed before any DDL.

    From columns alone: the old identity *is* the primary key, and the three inputs the
    application also folds in live inside the encrypted metadata blob. Read first and
    written after the column exists, so a failure while computing leaves a database this
    revision can still be run against.
    """
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT message_id, owner_id, agent_id FROM {_MESSAGES}")).fetchall()
    return [
        {"key": _dedup_key(owner_id, agent_id, message_id), "message_id": message_id}
        for message_id, owner_id, agent_id in rows
    ]


def _write_identities(lifted: list[dict[str, str]]) -> None:
    bind = op.get_bind()
    for row in lifted:
        bind.execute(sa.text(f"UPDATE {_MESSAGES} SET {_DEDUP}=:key WHERE message_id=:message_id"), row)
    log.info("0010 lifted %d message row(s) into the owner-carrying identity", len(lifted))


def _verify_unique() -> None:
    """Fail loudly rather than half-apply: a duplicate cannot take the index below.

    It cannot happen — the folded values are unique because the primary keys they fold
    were — but a database that has been repaired by hand is not covered by that argument.
    """
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(f"SELECT {_DEDUP}, COUNT(*) AS n FROM {_MESSAGES} GROUP BY {_DEDUP} HAVING COUNT(*) > 1")
    ).fetchall()
    if duplicates:
        named = ", ".join(str(row[0]) for row in duplicates[:10])
        raise RuntimeError(f"0010: {len(duplicates)} duplicate {_DEDUP} value(s) before indexing: {named}")


def _lift_message_identity() -> None:
    lifted = _lifted_identities()
    op.add_column(_MESSAGES, sa.Column(_DEDUP, sa.Text(), nullable=False, server_default=""))
    _write_identities(lifted)
    _verify_unique()
    op.create_index(_DEDUP_INDEX, _MESSAGES, [_DEDUP], unique=True)


# --- Deployment keyspace ------------------------------------------------------


def _create_deployment_keyspace() -> None:
    """One row for the database, because the runtime database *is* the deployment boundary.

    Two deployments meaning different keyspaces already need separate runtime databases, or
    they would be sharing owner runtime state too.
    """
    op.create_table(
        _KEYSPACE,
        # Always `_ONE_ROW`; the constraint is what makes a second recording impossible
        # rather than merely unlikely when two instances boot together.
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("bucket", sa.Text(), nullable=False),
        # Part of where bytes land: one bucket name at two endpoints is two buckets.
        sa.Column("endpoint", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


# --- Soft-timeout notice ------------------------------------------------------


def _add_soft_notice() -> None:
    """No backfill: a row written before the upgrade reads as "not yet noticed".

    For a run already past its soft timeout that costs at most one extra notice, once — and
    the alternative would be inventing a time nobody observed.
    """
    op.add_column(_TASKS, sa.Column("soft_notice_at", sa.Text(), nullable=True))


# --- Consolidation leases -----------------------------------------------------


def _create_consolidation_leases() -> None:
    """The two-column primary key *is* the single-flight rule.

    The way the unique partial index above is the one-running-root-task-per-owner rule. No
    backfill: an absent row means "unleased, never failed", which is exactly the state a
    fresh deployment and an upgraded one are both in.
    """
    op.create_table(
        _LEASES,
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        # Empty means unleased. The holder is an instance identity, not a worker lane: a
        # renewal and a release both name it, so a displaced run frees nothing.
        sa.Column("holder_id", sa.Text(), nullable=False, server_default=""),
        # ISO, compared textually against a `now` the caller passes — the shape every other
        # time predicate in this store already has, and the one that needs no per-dialect
        # clock expression.
        sa.Column("lease_expires_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failure_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("owner_id", "agent_id"),
    )


def upgrade() -> None:
    _preflight()
    _add_run_addressing()
    _rekey_owner_runtime_state()
    _attribute_spend_and_audit()
    _add_task_parentage()
    _lift_message_identity()
    _create_deployment_keyspace()
    _add_soft_notice()
    _create_consolidation_leases()


def downgrade() -> None:
    """Undo the steps in reverse. **Rows written under 0010 become unfindable by client id.**

    Not a symmetric rollback, and the asymmetry has to be stated. Messages that existed
    before this revision keep the primary keys the pre-change code re-derives, so they are
    reached exactly as they always were. A row *written* under 0010 has a random surrogate
    primary key instead, and the pre-change lookup derives a key that matches nothing: its
    duplicate pre-check misses, its insert no longer conflicts, and the journal resubmits
    it. Downgrade only a database that has taken no writes under this revision.
    """
    op.drop_table(_LEASES)
    op.drop_column(_TASKS, "soft_notice_at")
    op.drop_table(_KEYSPACE)

    op.drop_index(_DEDUP_INDEX, table_name=_MESSAGES)
    op.drop_column(_MESSAGES, _DEDUP)

    op.drop_index(_ONE_ROOT_INDEX, table_name=_TASKS)
    op.drop_index(_CLAIM_INDEX, table_name=_TASKS)
    op.drop_column(_TASKS, _PARENT)

    op.drop_index(_AUDIT_OWNER_INDEX, table_name=_AUDIT)
    op.drop_index(_USAGE_OWNER_INDEX, table_name=_USAGE)
    for table in (_AUDIT, _USAGE):
        op.drop_column(table, "agent_id")
        op.drop_column(table, "owner_id")

    op.rename_table(_STATE, _STATE_OLD)
    op.create_table(
        _STATE,
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    # One tenant row again, and the value it carries is the one the legacy owner held.
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {_STATE} (tenant_id, payload_json, updated_at) "
            f"SELECT :tenant_id, payload_json, updated_at FROM {_STATE_OLD} "
            "WHERE owner_id = :owner_id AND agent_id = :agent_id"
        ),
        {"tenant_id": "default", "owner_id": _DEFAULT_OWNER_ID, "agent_id": _DEFAULT_AGENT_ID},
    )
    op.drop_table(_STATE_OLD)

    op.drop_index(_SWEEP_INDEX, table_name=_TASKS)
    op.drop_column(_TASKS, "stop_requested_at")
    op.drop_column(_TASKS, "runtime_id")
