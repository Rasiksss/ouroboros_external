"""Прогресс станции доезжает до потребителя на другой реплике.

🔴 ЗАЧЕМ. Демон станции шлёт пачку по HTTP, и она попадает на ЛЮБОЙ воркер.
Потребитель зарегистрирован на том, где исполняется тул. Раньше это совпадало
по единственной причине — воркер был один. С несколькими прогресс уходил бы в
«no_consumer», а `done` не доезжал бы до почтового ящика: фоновое извлечение
молча теряло бы сводку, и на экране это выглядит как замерший счётчик.

⚠️ КАК ИЗОБРАЖАЕТСЯ ВТОРАЯ РЕПЛИКА. Потребитель регистрируется обычным путём, а
затем его очередь убирается из `_bus` — реестра ЭТОЙ реплики. Подписка на шину
при этом остаётся: ровно то, что видит соседний воркер, принявший пачку.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.api import station_progress_ingest as spi
from finengine.store import bus as B


@pytest.fixture(autouse=True)
def чистая_шина(monkeypatch, tmp_path):
    from finengine.config import settings

    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    B.reset_bus()
    spi._bus.clear()
    spi._подписки.clear()
    yield
    B.reset_bus()
    spi._bus.clear()
    spi._подписки.clear()


async def _принять(nonce: str, события: list[dict]) -> dict:
    return await spi.ingest(spi.ProgressBatch(nonce=nonce, events=события),
                            authorization="")


@pytest.mark.asyncio
async def test_a_consumer_on_another_replica_gets_the_progress():
    """🔴 ГЛАВНОЕ. Мутация: не публиковать пачку в шину — тест краснеет."""
    очередь = spi.register("n-1")
    await asyncio.sleep(0.05)                 # подписка на канал успевает встать
    свои = spi._bus.pop("n-1")                # «эта реплика о потребителе не знает»

    итог = await _принять("n-1", [{"type": "metric", "n": 40}])

    assert итог["reason"] == "delivered_elsewhere"
    assert итог["dropped"] == 0
    assert await asyncio.wait_for(очередь.get(), timeout=2.0) == {"type": "metric", "n": 40}
    spi._bus["n-1"] = свои


@pytest.mark.asyncio
async def test_a_local_consumer_gets_it_exactly_once():
    """На ОДНОЙ реплике событие приходит один раз, а не дважды.

    Локальная доставка осталась (она короче на сетевой круг), и публикация её
    дублировала бы: счётчик на экране считал бы вдвое. Мутация: убрать пометку
    `_local` — тест краснеет вторым экземпляром.
    """
    очередь = spi.register("n-2")
    await asyncio.sleep(0.05)

    await _принять("n-2", [{"type": "metric", "n": 1}])
    await asyncio.sleep(0.1)

    assert очередь.qsize() == 1, "событие пришло дважды — локально и из шины"


@pytest.mark.asyncio
async def test_every_consumer_of_the_nonce_gets_it():
    """Fan-out сохранён: у нонса два потребителя — ящик и релей на экран."""
    ящик = spi.register("n-3")
    релей = spi.register("n-3")
    await asyncio.sleep(0.05)

    await _принять("n-3", [{"type": "done"}])
    await asyncio.sleep(0.05)

    assert await asyncio.wait_for(ящик.get(), timeout=1.0) == {"type": "done"}
    assert await asyncio.wait_for(релей.get(), timeout=1.0) == {"type": "done"}


@pytest.mark.asyncio
async def test_nobody_anywhere_is_still_reported_as_a_loss():
    """🔴 «НЕТ ЗДЕСЬ» И «НЕТ НИГДЕ» — РАЗНЫЕ ВЕЩИ, И ВТОРОЕ ОБЯЗАНО КРИЧАТЬ.

    Прогресс — единственный признак жизни прогона на экране. Если он не достался
    никому, это отказ, а не норма. Мутация: считать «нет здесь» доставкой — тест
    краснеет: настоящая потеря стала бы выглядеть успехом.
    """
    итог = await _принять("никого", [{"type": "metric"}])

    assert итог == {"accepted": 0, "dropped": 1, "reason": "no_consumer"}


@pytest.mark.asyncio
async def test_unregistering_stops_the_bus_subscription():
    """Снятая регистрация снимает и подписку — иначе они копятся до конца процесса."""
    очередь = spi.register("n-4")
    await asyncio.sleep(0.05)
    assert id(очередь) in spi._подписки

    spi.unregister("n-4", очередь)
    await asyncio.sleep(0.05)

    assert id(очередь) not in spi._подписки
    assert "n-4" not in spi._bus
