"""WatchSubscription adapter over the existing Scenarios scheduler.

The scheduler owns *when* a subscription fires.  ``run_watch_subscription``
owns only the normal AIFA graph invocation; it does not extract, calculate or
diff through a second procedural monitoring pipeline.
"""
from __future__ import annotations

import json
import hashlib

from finengine.contracts import GoalIR, MonitoringCapacity, WatchSubscription
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.scenarios import store as scenario_store
from finengine.skills.spec.target import Target


def _numeric_owner(owner_id: str) -> int:
    try:
        value = int(owner_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("scenario-backed WatchSubscription requires the authenticated numeric owner id") from exc
    if value < 1:
        raise ValueError("owner id must be positive")
    return value


def _semantic_key(subscription: WatchSubscription) -> str:
    payload = {
        "subject_key": subscription.subject_key.value,
        "case_id": subscription.case_id,
        "check_type": subscription.check_type,
        "schedule": subscription.schedule.model_dump(mode="json"),
        "allowed_scope": sorted(subscription.allowed_scope),
        "policy_version": subscription.policy_version,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def create_watch_subscription(subscription: WatchSubscription) -> WatchSubscription:
    """Create one scheduler row, idempotently and within the owner boundary."""
    user_id = _numeric_owner(subscription.owner_id)
    semantic_key = _semantic_key(subscription)
    digest = hashlib.sha256(f"{user_id}|{semantic_key}".encode("utf-8")).hexdigest()
    stable_subscription_id = "watch:" + digest[:32]
    stable_scenario_id = "watch-" + digest[:32]
    subscription = subscription.model_copy(update={"subscription_id": stable_subscription_id})
    for scenario in scenario_store.list_for_user(user_id):
        if scenario.kind != "aifa_monitor":
            continue
        params = scenario.params_dict
        if params.get("idempotency_key") == subscription.idempotency_key or params.get("semantic_key") == semantic_key:
            return WatchSubscription.model_validate(params["subscription"])
    schedule = subscription.schedule
    freq = schedule.kind if schedule.kind in {"manual", "hourly", "daily", "weekly"} else "manual"
    scenario = scenario_store.create(
        user_id=user_id,
        name=f"AIFA monitor {subscription.subject_key.value[-8:]}",
        kind="aifa_monitor",
        description=f"{subscription.check_type}: {subscription.subject_key.value}",
        project_id=subscription.case_id,
        freq=freq,
        at_time=schedule.at_time or "09:00",
        dows=schedule.weekdays,
        paused=not subscription.active,
        scenario_id=stable_scenario_id,
    )
    stored = subscription.model_copy(update={"scenario_id": scenario.id})
    scenario_store.update(scenario.id, params={
        "idempotency_key": subscription.idempotency_key,
        "semantic_key": semantic_key,
        "subscription": stored.model_dump(mode="json"),
    })
    return stored


def get_watch_subscription(owner_id: str, scenario_id: str) -> WatchSubscription | None:
    scenario = scenario_store.get(scenario_id)
    if scenario is None or scenario.kind != "aifa_monitor" or scenario.user_id != _numeric_owner(owner_id):
        return None
    raw = scenario.params_dict.get("subscription")
    return WatchSubscription.model_validate(raw) if raw else None


def estimate_scheduled_runs_per_day(owner_id: str) -> float:
    total = 0.0
    for scenario in scenario_store.list_for_user(_numeric_owner(owner_id)):
        if scenario.kind != "aifa_monitor" or scenario.paused:
            continue
        if scenario.freq == "hourly":
            total += 24.0
        elif scenario.freq == "daily":
            total += 1.0
        elif scenario.freq == "weekly":
            total += len(scenario.dow_list or [0]) / 7.0
    return total


def measure_monitoring_capacity(case_store, owner_id: str) -> MonitoringCapacity:
    """Measure persisted history growth and active scheduled load for one owner."""
    scenarios = [
        scenario for scenario in scenario_store.list_for_user(_numeric_owner(owner_id))
        if scenario.kind == "aifa_monitor" and not scenario.paused
    ]
    return case_store.measure_subject_history(
        owner_id,
        active_subscriptions=len(scenarios),
        estimated_runs_per_day=estimate_scheduled_runs_per_day(owner_id),
    )


def run_watch_subscription(runtime: CaseRuntime, subscription: WatchSubscription):
    """Execute a scheduled snapshot-history watch through the ordinary CaseRuntime graph.

    Срез 32: чат и станция сравнивают периоды ОДНОГО отчёта (`PeriodChangeReport`), а
    подписка по расписанию по-прежнему сравнивает сохранённые снимки субъекта, поэтому
    её корень `MonitorResult` строится здесь напрямую, а не через `root_from_goal`."""
    case = runtime.store.load_case(subscription.owner_id, subscription.case_id)
    if case is None:
        raise KeyError(subscription.case_id)
    goal = GoalIR(
        raw_query=f"monitor {subscription.subject_key.value}",
        intent_kind="monitor_changes",
        parameters={
            "subject_key": subscription.subject_key.value,
            "policy_version": subscription.policy_version,
        },
    )
    root = Target(
        output_type="MonitorResult", entity_ref=subscription.subject_key.value,
        policy_version=subscription.policy_version,
        params={"document_scope": "subject_history"},
    )
    planned = runtime.plan(
        owner_id=subscription.owner_id, case_id=subscription.case_id,
        goal_ir=goal, target=root, run_passport=case.run_passport, root_extra={},
    )
    result = runtime.execute(
        owner_id=subscription.owner_id, case_id=subscription.case_id,
        plan_run_id=planned.record.plan_run_id,
    )
    return planned, result


__all__ = [
    "create_watch_subscription", "get_watch_subscription",
    "estimate_scheduled_runs_per_day", "measure_monitoring_capacity",
    "run_watch_subscription",
]
