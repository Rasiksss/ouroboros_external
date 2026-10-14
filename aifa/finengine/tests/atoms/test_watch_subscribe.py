from __future__ import annotations

import pytest

from finengine.config import settings
from finengine.contracts import WatchSchedule, WatchSubscription
from finengine.skills.atoms.subject_history import subject_key_from_identity
from finengine.skills.scenarios import scheduler, store as scenario_store
from finengine.skills.watch_subscriptions import (
    create_watch_subscription,
    estimate_scheduled_runs_per_day,
    get_watch_subscription,
    measure_monitoring_capacity,
)
from finengine.store.cases import SqlCaseStore
from tests.slice11_fixtures import metric, profile
from tests.slice11_fixtures import identity


def _subscription(owner="1", *, key="cmd-1"):
    return WatchSubscription(
        subject_key=subject_key_from_identity(identity()), owner_id=owner,
        case_id="case-1", check_type="snapshot_and_threshold",
        schedule=WatchSchedule(kind="daily", at_time="09:00"),
        allowed_scope=["REVENUE", "LEVERAGE"], policy_version="monitor-v1",
        idempotency_key=key,
    )


def test_subscription_reuses_scheduler_is_idempotent_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scenarios_database_url", f"sqlite:///{tmp_path / 'scenarios.db'}")
    scenario_store._engines.clear()
    scenario_store.init_schema()
    first = create_watch_subscription(_subscription())
    retry = create_watch_subscription(_subscription(key="another-command-same-semantics"))
    assert retry.scenario_id == first.scenario_id
    assert get_watch_subscription("1", first.scenario_id) == first
    assert get_watch_subscription("2", first.scenario_id) is None
    assert scenario_store.get(first.scenario_id).kind == "aifa_monitor"
    assert estimate_scheduled_runs_per_day("1") == 1.0
    case_store = SqlCaseStore(f"sqlite:///{tmp_path / 'cases.db'}")
    case_store.append_subject_profile(profile(1, [metric(100, period="2025-Q1")]))
    capacity = measure_monitoring_capacity(case_store, "1")
    assert capacity.snapshot_count == 1 and capacity.serialized_bytes > 0
    assert capacity.active_subscriptions == 1 and capacity.estimated_runs_per_day == 1.0
    scenario_store._engines.clear()


@pytest.mark.asyncio
async def test_event_subscription_dispatch_is_owner_scoped_and_event_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scenarios_database_url", f"sqlite:///{tmp_path / 'events.db'}")
    scenario_store._engines.clear()
    scenario_store.init_schema()
    subscription = _subscription().model_copy(update={
        "schedule": WatchSchedule(kind="event", event_type="new_filing"),
    })
    stored = create_watch_subscription(subscription)
    calls: list[str] = []

    async def fake_fire(scenario):
        calls.append(scenario.id)

    monkeypatch.setattr(scheduler, "_fire", fake_fire)
    assert await scheduler.fire_watch_event("new_filing", "filing-1", user_id=2) == []
    assert await scheduler.fire_watch_event("new_filing", "filing-1", user_id=1) == [stored.scenario_id]
    assert await scheduler.fire_watch_event("new_filing", "filing-1", user_id=1) == []
    assert calls == [stored.scenario_id]
    scenario_store._engines.clear()
