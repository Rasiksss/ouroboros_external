"""Owner runtime state persistence in the metadata DB."""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.storage.owner_state import (
    default_state_dict,
    ensure_state_defaults,
    load_owner_state_dict,
    owner_state_for_prompt,
)
from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.runtime_store import init_runtime_store
from supervisor.state import RuntimeStateRepository


def test_owner_state_save_load_roundtrip(tmp_path: pathlib.Path) -> None:
    store = init_runtime_store(tmp_path)
    repo = RuntimeStateRepository(tmp_path, store=store)
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
    store = init_runtime_store(tmp_path)
    assert store.load_owner_runtime_state() is None
    repo = RuntimeStateRepository(tmp_path, store=store)
    state = repo.load()
    assert state.session_id
    assert store.load_owner_runtime_state() is not None


def test_tenant_isolation(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = init_runtime_store(tmp_path)
    store.save_owner_runtime_state({"spent_calls": 1}, tenant_id="tenant-a")
    store.save_owner_runtime_state({"spent_calls": 2}, tenant_id="tenant-b")
    assert store.load_owner_runtime_state("tenant-a")["spent_calls"] == 1
    assert store.load_owner_runtime_state("tenant-b")["spent_calls"] == 2


def test_load_owner_state_dict_helper(tmp_path: pathlib.Path) -> None:
    store = init_runtime_store(tmp_path)
    store.save_owner_runtime_state({"spent_calls": 7})
    data = load_owner_state_dict(tmp_path)
    assert data["spent_calls"] == 7
    assert data["session_id"]


def test_load_owner_state_dict_defaults_without_row(tmp_path: pathlib.Path) -> None:
    init_runtime_store(tmp_path)
    data = load_owner_state_dict(tmp_path)
    assert data["spent_calls"] == default_state_dict()["spent_calls"]


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


def test_load_purges_unknown_keys_from_db(tmp_path: pathlib.Path) -> None:
    store = init_runtime_store(tmp_path)
    store.save_owner_runtime_state(
        {
            "spent_calls": 1,
            "session_id": "sess-1",
            "message_offset": 42,
            "approvals": [],
        }
    )
    repo = RuntimeStateRepository(tmp_path, store=store)
    repo.load()
    persisted = store.load_owner_runtime_state()
    assert persisted is not None
    assert persisted["spent_calls"] == 1
    assert "message_offset" not in persisted
    assert "approvals" not in persisted


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
