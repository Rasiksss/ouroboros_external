"""Шина сообщений: канал доносит событие до подписчика, и только до своего.

🔴 ЗАЧЕМ. `docs/rules/architecture.md` 3.2 (MUST): запрос не должен зависеть от
того, какая реплика его обслужила. Четыре места держат состояние в памяти
процесса, и все четыре упираются в одно: событие рождается на ОДНОЙ реплике, а
ждут его на ДРУГОЙ. Задачу между процессами не перенести — переносим её голос.

⚠️ REDIS-ВЕТКА ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ ЖИВЬЁМ: ей нужен поднятый Redis, а тесты
идут без сети. Проверяется контракт локальной ветки и переключатель. Живая
проверка Redis — смоук на стенде с двумя воркерами, и это сказано вслух, а не
спрятано за зелёным тестом.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.store import bus as B
from finengine.skills import run_orchestrator as ro


@pytest.fixture
def шина():
    return B.LocalBus()


@pytest.mark.asyncio
async def test_a_large_plan_serializes_event_publications(monkeypatch):
    """Сотни node_update одного PlanGraph не занимают сотни Redis-соединений."""
    class МедленнаяШина:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.seq: list[int] = []

        async def publish(self, _channel, message):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.seq.append(message["seq"])
            self.active -= 1
            return 1

    медленная = МедленнаяШина()
    monkeypatch.setattr(B, "get_bus", lambda: медленная)

    for seq in range(1, 201):
        ro._огласить("large-plan", seq, {"type": "plan_step"})

    хвост = ro._ХВОСТЫ_ПУБЛИКАЦИЙ[(asyncio.get_running_loop(), "events")]
    await хвост

    assert медленная.max_active == 1
    assert медленная.seq == list(range(1, 201))


@pytest.mark.asyncio
async def test_a_subscriber_receives_what_was_published(шина):
    with шина.subscribe("run:1") as подписка:
        await шина.publish("run:1", {"type": "metric", "n": 1})

        assert await подписка.get(timeout=1.0) == {"type": "metric", "n": 1}


@pytest.mark.asyncio
async def test_a_message_does_not_leak_into_another_channel(шина):
    with шина.subscribe("run:1") as первый, шина.subscribe("run:2") as второй:
        await шина.publish("run:1", {"n": 1})

        assert await первый.get(timeout=1.0) == {"n": 1}
        assert await второй.get(timeout=0.05) is None


@pytest.mark.asyncio
async def test_every_subscriber_of_a_channel_gets_the_message(шина):
    """Fan-out: у одного прогона бывает несколько потребителей.

    У станционного нонса их ровно два — почтовый ящик (сводка в историю) и
    релей живого прогресса на экран. Доставить одному значит потерять другого.
    """
    with шина.subscribe("run:1") as a, шина.subscribe("run:1") as b:
        await шина.publish("run:1", {"n": 1})

        assert await a.get(timeout=1.0) == {"n": 1}
        assert await b.get(timeout=1.0) == {"n": 1}


@pytest.mark.asyncio
async def test_a_closed_subscription_stops_receiving(шина):
    подписка = шина.subscribe("run:1")
    подписка.close()

    await шина.publish("run:1", {"n": 1})

    assert await подписка.get(timeout=0.05) is None


@pytest.mark.asyncio
async def test_publishing_with_nobody_listening_is_not_an_error(шина):
    """🔴 ШИНА НЕ ХРАНИТ, И ЭТО ЕЁ СВОЙСТВО, А НЕ ДЕФЕКТ.

    Подписчик, пришедший позже публикации, сообщения не увидит — так устроен и
    Redis-pub/sub. Отсюда правило: догон делается ЖУРНАЛОМ, шина отвечает
    только за хвост. Перепутать роли — значит потерять начало ответа у всех,
    кто подключился на секунду позже.
    """
    await шина.publish("run:пусто", {"n": 1})

    with шина.subscribe("run:пусто") as подписка:
        assert await подписка.get(timeout=0.05) is None


@pytest.mark.asyncio
async def test_a_slow_subscriber_does_not_stop_the_others(шина):
    """Переполненная очередь пропускает событие, остальные получают.

    Ронять публикацию из-за одного медленного потребителя нельзя: это
    остановило бы весь прогон ради одного зависшего экрана.
    """
    медленный = шина.subscribe("run:1")
    быстрый = шина.subscribe("run:1")
    for _ in range(B._MAXQ + 5):
        await шина.publish("run:1", {"n": 0})
    # Быстрый вычитывает, медленный — нет.
    прочитано = 0
    while await быстрый.get(timeout=0.01) is not None:
        прочитано += 1

    assert прочитано == B._MAXQ, "быстрый подписчик недополучил"
    медленный.close()
    быстрый.close()


@pytest.mark.asyncio
async def test_the_subscription_opens_before_it_is_awaited(шина):
    """🔴 ОТКРЫТЬ ХВОСТ И СЛУШАТЬ — РАЗНЫЕ МОМЕНТЫ.

    Подписчик обязан открыть хвост, ПОТОМ прочитать журнал (догон) и только
    потом слушать. Иначе события между чтением журнала и подпиской проваливаются
    в щель — «пропало ровно одно событие в середине», отказ, который ловится
    глазами и не воспроизводится.

    Мутация: сделать `subscribe` асинхронной и подписываться при первом `get` —
    тест краснеет: публикация между открытием и чтением теряется.
    """
    подписка = шина.subscribe("run:1")          # открыли
    await шина.publish("run:1", {"n": "во время догона"})  # случилось «в щели»
    await asyncio.sleep(0)                       # догон занял время

    assert await подписка.get(timeout=1.0) == {"n": "во время догона"}
    подписка.close()


def test_the_switch_defaults_to_the_local_bus(monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "redis_url", "")
    B.reset_bus()
    assert isinstance(B.get_bus(), B.LocalBus)
    B.reset_bus()


def test_many_workers_without_a_shared_bus_refuse_to_start(monkeypatch):
    """🔴 ТИХИЙ РАЗЪЕЗД ХУЖЕ ОТКАЗА ЗАПУСКА.

    Число воркеров задаётся в одном месте, общая шина — в другом. Поднять
    первое, забыв второе, значит вернуть разъезд: тулы отвечают «ход не
    зарегистрирован», поллинг «unknown ticket», подписка обрывается. Ничего
    из этого не выглядит отказом — выглядит как «агент сломался».

    Мутация: убрать проверку — тест краснеет, и вместе с ним исчезает
    единственное, что связывает две настройки.
    """
    from finengine.config import settings

    monkeypatch.setenv("FINENGINE_WORKERS", "4")
    monkeypatch.setattr(settings, "redis_url", "")
    with pytest.raises(RuntimeError, match="FINENGINE_REDIS_URL"):
        B.проверить_готовность_к_репликам()

    # С шиной — запускается.
    monkeypatch.setattr(settings, "redis_url", "redis://redis:6379/0")
    B.проверить_готовность_к_репликам()

    # Один воркер — шина не нужна, как было.
    monkeypatch.setenv("FINENGINE_WORKERS", "1")
    monkeypatch.setattr(settings, "redis_url", "")
    B.проверить_готовность_к_репликам()


# ── Redis-ветка: отказ в момент подписки не глушит канал ────────────────────
#
# 🔴 ПОЙМАНО РЕВИЗИЕЙ 27.08.2026 репродукцией на двойнике. Задачу подписки
# никто не await'ит, её исключение умирало молча — а канал уже лежал в
# `_каналы`, и повторный subscribe считал его взведённым. Одноразовый отказ
# Redis на старте делал канал глухим навсегда; для канала тикетов, где
# подписка одна на процесс, это вечный «running» у поллинга.


class _ПадающийPubSub:
    """Первые `отказов` подписок падают, дальше работает. Помнит успехи."""

    def __init__(self, отказов: int) -> None:
        self.осталось_отказов = отказов
        self.подписан: list[str] = []

    async def subscribe(self, channel):
        if self.осталось_отказов > 0:
            self.осталось_отказов -= 1
            raise ConnectionError("Redis ещё не поднялся")
        self.подписан.append(channel)

    async def unsubscribe(self, channel):
        self.подписан.remove(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(0.01)
        return None

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_a_failed_subscribe_retries_until_redis_returns(monkeypatch):
    """Мутация: убрать повтор из `_подписаться` — тест краснеет."""
    import types

    import redis.asyncio as _redis

    пубсаб = _ПадающийPubSub(отказов=1)
    клиент = types.SimpleNamespace(pubsub=lambda **_: пубсаб,
                                   publish=None, aclose=None)
    monkeypatch.setattr(_redis.Redis, "from_url", staticmethod(lambda *a, **k: клиент))
    monkeypatch.setattr(B, "_MAXQ", 8, raising=False)
    шина = B.RedisBus("redis://нет", prefix="fe:dev:")
    # Пауза повтора в бою 1 с — тесту хватит короткой.
    monkeypatch.setattr(asyncio, "sleep", _короткий_сон(asyncio.sleep))

    with шина.subscribe("run:1"):
        # Первый заход упал; повтор обязан довести подписку до Redis.
        for _ in range(50):
            if пубсаб.подписан:
                break
            await _настоящий_сон(0.02)
        assert пубсаб.подписан == ["fe:dev:run:1"], (
            "после отказа Redis подписка не перевзведена — канал глух")


@pytest.mark.asyncio
async def test_a_subscriber_gone_during_the_outage_stops_the_retry(monkeypatch):
    """Подписчик снялся, пока Redis лежал, — повтор не долбит Redis вечно."""
    import types

    import redis.asyncio as _redis

    пубсаб = _ПадающийPubSub(отказов=3)
    клиент = types.SimpleNamespace(pubsub=lambda **_: пубсаб,
                                   publish=None, aclose=None)
    monkeypatch.setattr(_redis.Redis, "from_url", staticmethod(lambda *a, **k: клиент))
    шина = B.RedisBus("redis://нет", prefix="fe:dev:")
    monkeypatch.setattr(asyncio, "sleep", _короткий_сон(asyncio.sleep))

    подписка = шина.subscribe("run:1")
    await _настоящий_сон(0.02)   # задача успевает упасть первый раз
    подписка.close()             # подписчик ушёл, канала в _каналы больше нет
    await _настоящий_сон(0.1)    # повторам дают время сдаться

    assert пубсаб.подписан == [], "повтор подписался на канал, который никому не нужен"


_настоящий_сон = asyncio.sleep


def _короткий_сон(наст):
    async def сон(с):
        await наст(min(с, 0.01))
    return сон
