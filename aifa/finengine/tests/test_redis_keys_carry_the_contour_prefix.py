"""Каждый ключ и каждый канал в Redis несёт приставку контура.

🔴 ЗАЧЕМ. Требование разработчика (26.08.2026) к сосуществованию dev и prod:
«каждый ключ реддиса через префикс». Redis — общий на хост; поставленные на
него без разных приставок, контуры делят пропуска нонсов и каналы прогонов.
Это не порча данных, а хуже: чужой ход ВИДЕН и перебиваем — событие prod'а
приезжает подписчику dev'а, потому что канал называется одинаково.

Проверяем не логику приставки, а ТО, ЧТО ДОЕХАЛО ДО КЛИЕНТА Redis: между
`RedisCache`/`RedisBus` и настоящим Redis подставлен двойник-стенограф. Снять
`_k()` с любого входа — тест краснеет; на этом он и проверен.

⚠️ Живого Redis здесь нет и не будет: тесты идут без сети. Двойник повторяет
ровно тот кусок API, которым мы пользуемся, — не весь Redis.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.config import Settings
from finengine.store import bus as B
from finengine.store import cache as C


class ЗаписнойRedis:
    """Синхронный двойник: помнит, под какими именами его звали."""

    def __init__(self, общий: dict) -> None:
        self.полка = общий
        self.имена: list[str] = []

    def get(self, key):
        self.имена.append(key)
        return self.полка.get(key)

    def set(self, key, value, px=None):
        self.имена.append(key)
        self.полка[key] = value

    def delete(self, key):
        self.имена.append(key)
        self.полка.pop(key, None)

    def scan_iter(self, шаблон):
        self.имена.append(шаблон)
        начало = шаблон[:-1] if шаблон.endswith("*") else шаблон
        return [k for k in self.полка if k.startswith(начало)]


class ЗаписнойPubSub:
    def __init__(self, стенограмма: list[str]) -> None:
        self.стенограмма = стенограмма

    async def subscribe(self, channel):
        self.стенограмма.append(f"subscribe {channel}")

    async def unsubscribe(self, channel):
        self.стенограмма.append(f"unsubscribe {channel}")

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        await asyncio.sleep(0.01)
        return None

    async def aclose(self):
        pass


class ЗаписнойRedisAsync:
    def __init__(self) -> None:
        self.стенограмма: list[str] = []

    def pubsub(self, ignore_subscribe_messages=True):
        return ЗаписнойPubSub(self.стенограмма)

    async def publish(self, channel, data):
        self.стенограмма.append(f"publish {channel}")
        return 1

    async def aclose(self):
        pass


@pytest.fixture
def полка():
    """Один Redis на оба контура — ровно та беда, от которой защищаемся."""
    return {}


@pytest.fixture
def кэш(monkeypatch, полка):
    def сделать(приставка):
        import redis
        записной = ЗаписнойRedis(полка)
        monkeypatch.setattr(redis.Redis, "from_url",
                            staticmethod(lambda *a, **k: записной))
        к = C.RedisCache("redis://нет", prefix=приставка)
        return к, записной
    return сделать


def test_every_key_reaching_redis_carries_the_prefix(кэш):
    к, записной = кэш("fe:dev:")

    к.set("nonce:abc", {"run": 1}, ttl_sec=10)
    к.get("nonce:abc")
    к.delete("nonce:abc")
    к.keys("nonce:")

    assert записной.имена, "двойник не звали — тест ничего не проверил"
    чужие = [и for и in записной.имена if not и.startswith("fe:dev:")]
    assert not чужие, f"в Redis ушли имена без приставки контура: {чужие}"


def test_one_contour_does_not_see_the_keys_of_another(кэш):
    dev, _ = кэш("fe:dev:")
    prod, _ = кэш("fe:prod:")

    dev.set("nonce:abc", {"чей": "dev"}, ttl_sec=10)

    assert prod.get("nonce:abc") is None, "prod прочитал пропуск dev'а"
    assert prod.keys("") == [], "prod увидел ключи dev'а в перечислении"
    assert dev.get("nonce:abc") == {"чей": "dev"}, "dev потерял свой же ключ"


def test_a_prefix_without_a_separator_would_swallow_the_neighbour():
    """Приставка без двоеточия — не косметика, а дыра в изоляции.

    `fe:` без разделителя — начало `fe:dev:`, и перечисление одного контура
    вычерпывает ключи другого. Настройка дописывает двоеточие сама.
    """
    assert Settings(FINENGINE_REDIS_PREFIX="fe:dev").redis_prefix == "fe:dev:"
    assert Settings(FINENGINE_REDIS_PREFIX="fe:dev:").redis_prefix == "fe:dev:"
    assert Settings(FINENGINE_REDIS_PREFIX="").redis_prefix == ""


def test_the_prefix_comes_from_the_setting_not_from_the_code(monkeypatch, полка):
    """Приставка, зашитая в код, одинакова у обоих контуров — то есть её нет."""
    import redis
    записной = ЗаписнойRedis(полка)
    monkeypatch.setattr(redis.Redis, "from_url",
                        staticmethod(lambda *a, **k: записной))
    monkeypatch.setattr(C.settings, "redis_prefix", "fe:свой:")

    C.RedisCache("redis://нет").set("k", 1, ttl_sec=10)

    assert записной.имена == ["fe:свой:k"]


@pytest.mark.asyncio
async def test_every_channel_reaching_redis_carries_the_prefix(monkeypatch):
    import redis.asyncio as _redis
    записной = ЗаписнойRedisAsync()
    monkeypatch.setattr(_redis.Redis, "from_url",
                        staticmethod(lambda *a, **k: записной))

    шина = B.RedisBus("redis://нет", prefix="fe:dev:")
    with шина.subscribe("run:1"):
        await шина.publish("run:1", {"n": 1})
        await asyncio.sleep(0.05)  # задача подписки успевает сходить в Redis

    await asyncio.sleep(0.05)  # и задача отписки тоже

    assert записной.стенограмма, "двойник не звали — тест ничего не проверил"
    голые = [с for с in записной.стенограмма if " fe:dev:run:1" not in с]
    assert not голые, f"в Redis ушли каналы без приставки контура: {голые}"


@pytest.mark.asyncio
async def test_two_contours_do_not_share_a_channel_name(monkeypatch):
    import redis.asyncio as _redis
    записной = ЗаписнойRedisAsync()
    monkeypatch.setattr(_redis.Redis, "from_url",
                        staticmethod(lambda *a, **k: записной))

    await B.RedisBus("redis://нет", prefix="fe:dev:").publish("run:1", {})
    await B.RedisBus("redis://нет", prefix="fe:prod:").publish("run:1", {})

    assert записной.стенограмма == ["publish fe:dev:run:1", "publish fe:prod:run:1"]
