"""In-process scheduler for Scenarios («Сценарии»).

Spawned as a background asyncio task at FastAPI startup (alongside the
news-digest scheduler). Polls every 30 seconds; for each active scenario
whose trigger time matches the current MSK minute and which hasn't run
yet today, fires it headlessly via `runner.run_scenario`.

Idempotency comes from the DB, not an in-memory guard: `run_scenario`
stamps `last_run_at=now` the moment it starts, so the next tick (and a
restart mid-minute) sees "already ran today" and skips. Errors in one
scenario never stop the loop.

Configurable via env:
    FINENGINE_SCENARIOS_ENABLED   "1"/"0" (default "1")
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from finengine.skills.scenarios import runner, store

log = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


def _enabled() -> bool:
    return os.environ.get("FINENGINE_SCENARIOS_ENABLED", "1") == "1"


def _ran_in_period(scn: store.Scenario, now_msk: datetime, period: str) -> bool:
    """Idempotency guard: did this scenario already run in the current period?
    period="hour" for hourly, "day" for daily/weekly."""
    if not scn.last_run_at:
        return False
    try:
        last = datetime.fromtimestamp(scn.last_run_at, _MSK)
    except (ValueError, OSError, OverflowError):
        return False
    if period == "hour":
        return last.date() == now_msk.date() and last.hour == now_msk.hour
    return last.date() == now_msk.date()


def _is_due(scn: store.Scenario, now_msk: datetime) -> bool:
    """True if `scn` should fire at this MSK minute and hasn't run this period."""
    # «Вручную» — значит вручную (21.08.2026). Отбор в `list_all_active`
    # действительно не отдаёт сюда manual, но защита обязана быть и здесь:
    # у всех шести живых сценариев прода freq=manual при at_time=09:00, и
    # функция, вызванная в обход запроса, объявила бы их подлежащими запуску —
    # то есть выпуски пошли бы у людей, которые автозапуск не включали.
    if scn.freq not in ("hourly", "daily", "weekly"):
        return False
    # Parse "HH:MM".
    try:
        hh, mm = scn.at_time.split(":")
        trig_h, trig_m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return False
    if scn.freq == "hourly":
        # Fire once per hour at the configured minute-of-hour.
        if now_msk.minute != trig_m:
            return False
        return not _ran_in_period(scn, now_msk, "hour")
    # daily / weekly — exact hour+minute, once per day.
    if now_msk.hour != trig_h or now_msk.minute != trig_m:
        return False
    if scn.freq == "weekly" and now_msk.weekday() not in scn.dow_list:
        return False
    return not _ran_in_period(scn, now_msk, "day")


async def _fire(scn: store.Scenario) -> None:
    log.info("scenarios: firing id=%s kind=%s name=%r", scn.id, scn.kind, scn.name)
    try:
        async for _ev in runner.run_scenario(scn):
            pass  # headless — run_scenario records status + saves the artifact
        fresh = store.get(scn.id)
        log.info(
            "scenarios: done id=%s status=%s run=%s",
            scn.id, fresh.last_status if fresh else "?",
            (fresh.last_run_id[:8] if fresh and fresh.last_run_id else "-"),
        )
    except Exception as e:  # noqa: BLE001 — defensive; run_scenario already catches
        log.exception("scenarios: run crashed id=%s: %s", scn.id, e)


async def fire_watch_event(event_type: str, event_id: str, *, user_id: int | None = None) -> list[str]:
    """Dispatch an external event to matching watch scenarios exactly once.

    The event id is persisted in the existing scenario params *before* the
    run, so retries observed by later dispatcher passes are skipped.
    Database-level duplicate subscriptions are prevented separately by
    ``create_watch_subscription``.
    """
    fired: list[str] = []
    for scn in store.list_unpaused_kind("aifa_monitor"):
        if user_id is not None and scn.user_id != int(user_id):
            continue
        params = scn.params_dict
        raw = params.get("subscription") or {}
        schedule = raw.get("schedule") or {}
        if schedule.get("kind") != "event" or schedule.get("event_type") != event_type:
            continue
        if params.get("_last_event_id") == event_id:
            continue
        params["_last_event_id"] = event_id
        store.update(scn.id, params=params)
        await _fire(scn)
        fired.append(scn.id)
    return fired


async def run_forever() -> None:
    """Main scheduler loop — started as a background task in FastAPI startup."""
    if not _enabled():
        log.info("scenarios: scheduler disabled via FINENGINE_SCENARIOS_ENABLED")
        return

    log.info("scenarios: scheduler started (poll 30s, MSK)")
    while True:
        try:
            now_msk = datetime.now(_MSK)
            for scn in store.list_all_active():
                if _is_due(scn, now_msk):
                    await _fire(scn)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            log.info("scenarios: scheduler cancelled — shutting down")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("scenarios: loop iteration crashed: %s", e)
            await asyncio.sleep(60)
