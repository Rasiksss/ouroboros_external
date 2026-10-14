from pathlib import Path

import pytest
from sqlalchemy import text

from ouroboros.storage.runtime_models import RuntimeSession
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from supervisor.state import RuntimeStateRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_sessions import sql_runtime_session_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository


@parametrize_from_dict(
    {
        "transport owner": {"owner_id": 42, "owner_scope": TEST_SCOPE.to_dict(), "expected_owner": "42"},
        "chat fallback": {"owner_id": None, "owner_scope": TEST_SCOPE.to_dict(), "expected_owner": TEST_SCOPE.chat_id},
        "unclaimed": {"owner_id": None, "owner_scope": None, "expected_owner": ""},
    }
)
def test_sync_uses_injected_session_repository(
    tmp_path: Path, owner_id: int | None, owner_scope: dict[str, str] | None, expected_owner: str
) -> None:
    states = sql_owner_state_repository()
    states.save(
        OwnerStateDocument({"session_id": "existing", "owner_id": owner_id, "owner_scope": owner_scope}), TEST_SCOPE
    )
    state = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=states,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    sessions = sql_runtime_session_repository()

    state.sync_runtime_session(session_repository=sessions, source="dialogs")

    with runtime_persistence.current.engine.connect() as conn:
        saved = RuntimeSession.model_validate_json(
            conn.execute(text("SELECT payload_json FROM sessions WHERE session_id='existing'")).scalar_one()
        )
    assert (saved.source, saved.owner_external_id, saved.status) == ("dialogs", expected_owner, "active")
    assert state.load().session_id == "existing"


def test_sync_persists_a_new_session_id_before_writing_session_metadata(tmp_path: Path) -> None:
    states = sql_owner_state_repository()
    state = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=states,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    sessions = sql_runtime_session_repository()
    with (
        fail_sql("INSERT INTO sessions", OSError("session metadata unavailable")),
        pytest.raises(OSError, match="session metadata unavailable"),
    ):
        state.sync_runtime_session(session_repository=sessions)

    session_id = state.load().session_id
    assert session_id
    state.sync_runtime_session(session_repository=sessions)
    with runtime_persistence.current.engine.connect() as conn:
        assert conn.execute(text("SELECT session_id, source FROM sessions")).all() == [(session_id, "unknown")]
