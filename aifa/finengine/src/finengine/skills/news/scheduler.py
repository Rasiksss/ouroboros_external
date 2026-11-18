"""In-process scheduler for Daily News.

Spawned as a background asyncio task at FastAPI startup. Polls every
30 seconds; when now()-in-MSK crosses 17:30 and we haven't run for
today, fires the pipeline. Idempotent (sees its own last_run_date so
even if you restart at 17:30:01 you don't double-fire).

Configurable via env:
    FINENGINE_NEWS_DIGEST_TRIGGER_MSK   "HH:MM" (default "17:30")
    FINENGINE_NEWS_DIGEST_ENABLED        "1"/"0" (default "1" in prod)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")


def _enabled() -> bool:
    return os.environ.get("FINENGINE_NEWS_DIGEST_ENABLED", "1") == "1"


# 🔴 ЗДЕСЬ БЫЛ ПОЛНЫЙ ПРОГОН ДАЙДЖЕСТА — И ЕГО НЕ ЧИТАЛ НИКТО (21.08.2026).
#
# Планировщик звал `run_daily_news_pipeline()` БЕЗ АРГУМЕНТОВ: окно всегда 48
# часов, настройки сценариев игнорировались, владелец прогона — None. Пока
# чтение готового выпуска было общим, эта работа хотя бы кому-то доставалась;
# после привязки кэша к владельцу его выпуск не отдаётся никому вовсе — все
# три читателя передают реального пользователя.
#
# То есть ежедневно оплачивалась классификация (~$0.07 за прогон) ради
# markdown, который писался в базу и не показывался. Полезной оставалась одна
# часть — прогрев `raw_items`: собранное сырьё ускоряет последующие ручные
# прогоны пользователей. Её и оставляем.
#
# Расписание САМИХ выпусков живёт в сценариях (`skills/scenarios/scheduler.py`,
# freq=hourly|daily|weekly у каждого владельца со своими параметрами) — второго
# места для него быть не должно.
_WARM_WINDOW_H = 48   # то же окно, что раньше брал прогон по умолчанию


def _trigger() -> dtime:
    raw = os.environ.get("FINENGINE_NEWS_DIGEST_TRIGGER_MSK", "17:30")
    try:
        hh, mm = raw.split(":")
        return dtime(hour=int(hh), minute=int(mm))
    except (ValueError, IndexError):
        log.warning("scheduler: bad trigger %r, falling back to 17:30 MSK", raw)
        return dtime(hour=17, minute=30)


async def run_forever() -> None:
    """Main scheduler loop. Started as a background task in FastAPI
    `lifespan`. Loops until the event loop is cancelled."""
    if not _enabled():
        log.info("scheduler: disabled via FINENGINE_NEWS_DIGEST_ENABLED")
        return

    trigger = _trigger()
    log.info("scheduler: daily news pipeline trigger set to %s MSK", trigger)

    last_run_date: str | None = None
    while True:
        try:
            now_msk = datetime.now(_MSK)
            today = now_msk.date().isoformat()

            # Window: the trigger minute exactly. Sleep 30s means we
            # see this window once or twice — last_run_date guards.
            if (now_msk.hour == trigger.hour
                    and now_msk.minute == trigger.minute
                    and last_run_date != today):
                log.info("scheduler: прогрев кэша новостей (%s MSK)", now_msk)
                last_run_date = today  # set before the await so retry doesn't double-fire
                try:
                    from finengine.skills.news.collect import collect_all
                    items = await collect_all(since_hours=_WARM_WINDOW_H)
                    log.info("scheduler: прогрев готов, сырья %d", len(items))
                except Exception as e:  # noqa: BLE001
                    log.exception("scheduler: прогрев кэша упал: %s", e)
                    # Don't reset last_run_date — wait for next day.
                    # Ops can curl the on-demand endpoint to retry today.

            await asyncio.sleep(30)
        except asyncio.CancelledError:
            log.info("scheduler: cancelled — shutting down")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("scheduler: loop iteration crashed: %s", e)
            await asyncio.sleep(60)
