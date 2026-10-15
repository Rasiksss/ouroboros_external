"""Дренаж считает МОЛЧАНИЕ прогона, а не его срок жизни (19.08.2026).

ДЕФЕКТ, НАЙДЕННЫЙ НА ПРОГОНЕ ВЛАДЕЛЬЦА ПО ОТЧЁТУ НЕСТЛЕ. Дедлайн отсчитывался
от ВЗВОДА КАНАЛА:

    канал взведён            14:14:54
    первое событие станции   14:20:31   ← 5,5 мин на полку, распознавание, распаковку
    дедлайн (взвод + 20 мин) 14:34:54   ← прогон объявлен мёртвым
    процесс в комнате        жив и работает, сжатие идёт 16-ю минуту

Отчёт Нестле — 476 КБ текста; одно только сжатие контекста шло дольше, чем весь
отпущенный срок. Прогон похоронили живым, и всё, что он извлёк дальше, приехало
в закрытую очередь.

ПОЧЕМУ ИМЕННО МОЛЧАНИЕ. Срок жизни — плохая мера: он наказывает за тяжёлый
документ и за медленную подготовку, то есть ровно за то, в чём прогон не
виноват. Молчание — хорошая: пока события идут, комната работает, сколько бы
это ни длилось. А сбежавшую комнату держит абсолютный потолок.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ:
  * убрать сдвиг дедлайна на событии → `test_a_talking_run_outlives_the_window`;
  * убрать `hard_stop` → `test_a_runaway_room_is_stopped_by_the_ceiling`.
"""
from __future__ import annotations

import asyncio

from finengine.skills import station_run_sink


def _drain(events, *, timeout, gap=0.0, tail=None):
    """Прогнать события через боевой дренаж. `gap` — пауза между ними."""
    seen: dict = {"done": None, "abandoned": None, "events": 0}

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()

        async def feed():
            for ev in events:
                await asyncio.sleep(gap)
                queue.put_nowait(ev)
            if tail is not None:
                await asyncio.sleep(tail)

        task = asyncio.create_task(feed())
        await station_run_sink.drain(
            nonce="n", queue=queue, owner_id=1, timeout=timeout,
            on_event=lambda ev: seen.__setitem__("events", seen["events"] + 1),
            on_done=lambda run: seen.__setitem__("done", run),
            on_failed=lambda m: None,
            on_abandoned=lambda run: seen.__setitem__("abandoned", run))
        task.cancel()

    asyncio.run(scenario())
    return seen


def test_a_talking_run_outlives_the_window():
    """ГЛАВНОЕ: прогон, который говорит, живёт дольше окна молчания.

    Четыре события с паузой в 0,4 окна каждое — суммарно 1,6 окна. По прежнему
    правилу прогон умер бы на третьем; по новому доживает до терминала.
    """
    W = 0.25
    seen = _drain(
        [{"type": "status", "stage": "two_stage_compression"},
         {"type": "status", "stage": "two_stage_extraction"},
         {"type": "metric", "enum": "NET_PROFIT", "value_current": 1.0, "found": True},
         {"type": "done", "result": {"company_name": "Нестле"}}],
        timeout=W, gap=W * 0.4)

    assert seen["done"] is not None, (
        "прогон похоронен живым: события шли, а дедлайн считал общий срок")
    assert seen["abandoned"] is None


def test_silence_still_ends_the_run():
    """Замолчал — закрываем. Иначе мёртвая комната держала бы подписку."""
    W = 0.25
    seen = _drain(
        [{"type": "metric", "enum": "NET_PROFIT", "value_current": 1.0, "found": True}],
        timeout=W, gap=0.0, tail=W * 3)

    assert seen["done"] is None
    assert seen["abandoned"] is not None, "молчание перестало закрывать прогон"
    assert seen["abandoned"].metrics, "собранные строки потеряны при закрытии"


def test_a_runaway_room_is_stopped_by_the_ceiling(monkeypatch):
    """Комната, тараторящая без конца, всё равно упирается в потолок.

    Без потолка «жив, пока говорит» превращается в «жив всегда»: подписка
    висит, канал не снимается, а результата нет.
    """
    monkeypatch.setattr(station_run_sink, "_HARD_STOP_FACTOR", 2.0)
    W = 0.15
    # Событий больше, чем помещается в потолок: 20 × 0.5W = 10W при потолке 2W.
    seen = _drain([{"type": "status", "stage": "болтовня"}] * 20,
                  timeout=W, gap=W * 0.5)

    assert seen["abandoned"] is not None, (
        "потолок не сработал — сбежавшая комната держала бы подписку вечно")
    assert seen["events"] < 20, "дренаж досидел до конца потока, потолка нет"


def test_the_ceiling_is_generous_enough_for_real_work():
    """Потолок не должен рубить обычный тяжёлый прогон.

    Окно молчания у нас двадцать минут; потолок втрое — час. Отчёт Нестле,
    самый тяжёлый из виденных, укладывается с запасом.
    """
    from finengine.skills.grounding_bus import WORKER_LIFETIME_SEC

    ceiling = WORKER_LIFETIME_SEC * station_run_sink._HARD_STOP_FACTOR
    assert ceiling >= 3600, (
        f"потолок {ceiling:.0f} с — меньше часа. Прогон Нестле шёл дольше "
        f"двадцати минут только на сжатии")
