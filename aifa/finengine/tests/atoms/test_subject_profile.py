from __future__ import annotations

import pytest

from finengine.contracts import EntityIdentity
from finengine.skills.atoms.subject_history import subject_key_from_identity
from finengine.store.cases import SqlCaseStore
from tests.slice11_fixtures import identity, metric, profile


def test_same_verified_identifier_survives_document_language_and_name_spelling():
    ru = identity(name="ПАО Эй-Си-Эм-И")
    en = identity(name="ACME plc")
    assert subject_key_from_identity(ru) == subject_key_from_identity(en)


def test_same_name_different_issuer_is_not_merged():
    first = identity("5493001KJTIIGC8Y1R12", name="Twin Bank")
    second = identity("213800D1EI4B9WTWWD28", name="Twin Bank")
    assert subject_key_from_identity(first).value != subject_key_from_identity(second).value


def test_company_name_without_identifier_cannot_become_subject_key():
    only_name = EntityIdentity(entity_id="ent-name", canonical_name="ACME", raw_name="ACME")
    with pytest.raises(ValueError, match="name alone"):
        subject_key_from_identity(only_name)


def test_subject_history_is_append_only_idempotent_and_owner_scoped(tmp_path):
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'history.db'}")
    first = store.append_subject_profile(profile(1, [metric(100, period="2025-Q1")]))
    second = store.append_subject_profile(profile(1, [metric(120, period="2025-Q2")], plan_run_id="plan-2"))
    retry = store.append_subject_profile(profile(99, [metric(999, period="2025-Q2")], plan_run_id="plan-2"))
    assert (first.version, second.version) == (1, 2)
    assert second.previous_snapshot_id == first.snapshot_id
    assert retry.snapshot_id == second.snapshot_id
    assert store.list_subject_profiles("2", first.subject_key.value) == []
    loaded = store.list_subject_profiles("1", first.subject_key.value)
    assert [p.metrics[0].scalar_fact.value for p in loaded] == [100, 120]
    capacity = store.measure_subject_history("1", active_subscriptions=3, estimated_runs_per_day=25)
    assert capacity.snapshot_count == 2 and capacity.serialized_bytes > 0
    assert capacity.active_subscriptions == 3 and capacity.estimated_runs_per_day == 25


def test_slice11_payload_round_trips_and_is_frozen():
    original = profile(1, [metric(100, period="2025-Q1")])
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original
    with pytest.raises(Exception):
        restored.version = 2
