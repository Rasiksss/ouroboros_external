"""Owner runtime state normalization and session lifecycle."""

from __future__ import annotations

import pathlib
from unittest.mock import Mock

from ouroboros.storage.owner_state import (
    default_state_dict,
    ensure_state_defaults,
    owner_state_for_prompt,
)
from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.interfaces.owner_state import OwnerStateDocument
from supervisor.state import RuntimeStateRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository
from transport.dialogs.infra.repositories.repo import SqlDialogsCursorRepository


def test_owner_state_save_load_roundtrip(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    state = OwnerRuntimeState.model_validate(
        {
            "owner_id": 42,
            "spent_calls": 3,
            "session_id": "sess-abc",
        }
    )
    repo.save(state)
    loaded = repo.load()
    assert loaded.owner_id == 42
    assert loaded.spent_calls == 0
    assert loaded.session_id == "sess-abc"


def test_load_creates_default_row_when_missing(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    assert owners.load(TEST_SCOPE) is None
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    state = repo.load()
    assert state.session_id
    assert owners.load(TEST_SCOPE) is not None


def test_coerce_owner_state_drops_unknown_keys() -> None:
    cleaned = ensure_state_defaults(
        {
            "owner_id": 1,
            "message_offset": 99,
            "tg_offset": 5,
            "current_branch": "main",
            "evolution_mode_enabled": True,
            "openrouter_daily_usd": 1.0,
        }
    )
    assert cleaned["owner_id"] == 1
    assert "message_offset" not in cleaned
    assert "tg_offset" not in cleaned
    assert "current_branch" not in cleaned
    assert "evolution_mode_enabled" not in cleaned
    assert "openrouter_daily_usd" not in cleaned


def test_load_purges_unknown_keys_from_repository(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    owners.save(
        OwnerStateDocument(
            {
                "spent_calls": 1,
                "session_id": "sess-1",
                "message_offset": 42,
                "approvals": [],
            }
        ),
        TEST_SCOPE,
    )
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    repo.load()
    persisted = owners.load(TEST_SCOPE).payload
    assert persisted is not None
    assert persisted["spent_calls"] == 1
    assert "message_offset" not in persisted
    assert "approvals" not in persisted


def test_a_row_created_by_a_provider_cursor_keeps_one_session_id(tmp_path: pathlib.Path) -> None:
    """The dialogs cursor can write the owner row first; reading it must not remint the session."""
    store = runtime_persistence.initialize()
    SqlDialogsCursorRepository(store.engine, scope=TEST_SCOPE).save_cursor(7)
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=sql_owner_state_repository(store.engine),
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    first = repo.load().session_id
    second = repo.load().session_id

    assert first
    assert first == second
    assert sql_owner_state_repository(store.engine).load(TEST_SCOPE).payload["provider_state"] == {
        "dialogs": {"seq": 7}
    }


def test_load_persists_the_session_id_it_had_to_mint(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    owners.save(OwnerStateDocument({"spent_calls": 1}), TEST_SCOPE)
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    minted = repo.load().session_id

    assert minted
    assert owners.load(TEST_SCOPE).payload["session_id"] == minted
    assert repo.load().session_id == minted


def test_reading_the_session_id_costs_no_usage_aggregate(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    owners.save(OwnerStateDocument({**default_state_dict(), "session_id": "sess-1"}), TEST_SCOPE)
    usage = Mock(wraps=sql_usage_repository())
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=usage,
        audit_event_repository=sql_audit_event_repository(),
    )

    assert repo.session_id() == "sess-1"
    assert len(usage.sum_usage_stats.call_args_list) == 0
    assert repo.load().session_id == "sess-1"
    assert len(usage.sum_usage_stats.call_args_list) == 1


def test_session_id_falls_back_to_load_when_the_row_lacks_one(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    owners.save(OwnerStateDocument({"spent_calls": 1}), TEST_SCOPE)
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=TEST_SCOPE,
        usage_repository=sql_usage_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )

    minted = repo.session_id()

    assert minted
    assert repo.session_id() == minted


def test_owner_state_for_prompt_omits_unknown_fields() -> None:
    prompt_view = owner_state_for_prompt(
        {
            "owner_id": 7,
            "spent_calls": 2,
            "message_offset": 1,
            "created_at": "2020-01-01",
        }
    )
    assert prompt_view["owner_id"] == 7
    assert prompt_view["spent_calls"] == 2
    assert "message_offset" not in prompt_view
    assert "created_at" not in prompt_view
