from pathlib import Path

from sqlalchemy import event

from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint
from supervisor.interfaces.owner_state import OwnerStateDocument
from supervisor.state import RuntimeStateRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.cleanup_checkpoint import sql_cleanup_checkpoint_repository
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def test_state_uses_the_injected_repository_for_reads_writes_and_other_scopes(tmp_path: Path) -> None:
    owners = sql_owner_state_repository()
    other = scope_with(TEST_SCOPE, owner_id="other")
    owners.save(OwnerStateDocument({"session_id": "mine"}), TEST_SCOPE)
    owners.save(OwnerStateDocument({"session_id": "theirs"}), other)
    state = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=sql_usage_repository(),
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )

    loaded = state.load()
    loaded.owner_id = 42
    state.save(loaded)

    assert state.session_id() == "mine"
    assert state.for_scope(other).session_id() == "theirs"
    persisted = owners.load(TEST_SCOPE)
    assert persisted is not None and persisted.payload["owner_id"] == 42
    assert owners.load(other).payload["session_id"] == "theirs"


def test_state_save_preserves_transport_progress_after_loading_a_stale_snapshot(tmp_path: Path) -> None:
    owners = sql_owner_state_repository()
    state = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        usage_repository=sql_usage_repository(),
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )
    stale = state.load()
    SqlDialogsCursorRepository(runtime_persistence.current.engine, scope=TEST_SCOPE).save_cursor(17)
    stale.owner_id = 42

    state.save(stale)

    saved = state.load()
    assert saved.provider_state == {"dialogs": {"seq": 17}}
    assert saved.owner_id == 42


def test_normalization_and_save_keep_cleanup_checkpoint_without_repeated_writes(tmp_path: Path) -> None:
    engine = runtime_persistence.current.engine
    state = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(engine),
        usage_repository=sql_usage_repository(),
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )
    stale = state.load()
    checkpoints = sql_cleanup_checkpoint_repository(engine)
    checkpoint = CleanupCheckpoint("2026-07-01T00:00:00+00:00", {"scratchpad_blocks": 7})
    checkpoints.save_checkpoint(checkpoint, TEST_SCOPE)

    stale.owner_id = 42
    state.save(stale)
    assert checkpoints.get_last_run_at(TEST_SCOPE) == checkpoint.last_run_at
    writes = []

    def record_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith(("INSERT INTO owner_runtime_state", "UPDATE owner_runtime_state")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record_write)
    try:
        assert state.load().owner_id == 42
        assert state.load().owner_id == 42
    finally:
        event.remove(engine, "before_cursor_execute", record_write)

    assert checkpoints.get_last_run_at(TEST_SCOPE) == checkpoint.last_run_at
    assert writes == []
