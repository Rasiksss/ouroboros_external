"""Шина сообщений: канал → подписчики. Внутри процесса или через Redis.

ЗАЧЕМ. `docs/rules/architecture.md` 3.2 (🔴 MUST): «Запрос не должен зависеть
от того, какая реплика его обслужила. In-process синглтоны… разъезжающиеся на
`>1` реплике — баги».

Это основание для снятия `--workers 1`. Четыре места держат состояние в памяти
процесса, и все четыре упираются в одно: событие рождается на ОДНОЙ реплике, а
ждут его на ДРУГОЙ. Перенести сам `asyncio.Task` между процессами нельзя —
значит переносить надо не задачу, а её ГОЛОС.

🔴 ЧЕГО ЭТА ШИНА НЕ ДЕЛАЕТ, И ЭТО ВАЖНЕЕ ТОГО, ЧТО ДЕЛАЕТ.

  * ОНА НЕ ХРАНИТ. Подписчик, пришедший после публикации, сообщения не увидит:
    и `LocalBus`, и Redis-pub/sub раздают только живым. Поэтому догон
    («покажи, что уже было») делается ЖУРНАЛОМ, а шина отвечает лишь за
    хвост. Перепутать эти две роли — значит потерять начало ответа у любого,
    кто подключился на секунду позже;
  * ПУБЛИКАЦИЯ СООБЩАЕТ ЧИСЛО ПОЛУЧАТЕЛЕЙ, и это не украшение. Без него
    «подписчика нет ЗДЕСЬ» неотличимо от «событие пропало НИГДЕ» — а первое
    норма при нескольких репликах, второе отказ, о котором надо кричать.
    Redis отвечает тем же числом на `PUBLISH`;
  * ОНА НЕ ГАРАНТИРУЕТ ДОСТАВКУ. Redis-pub/sub — «отправил и забыл»: обрыв
    связи с Redis теряет сообщения окна. Поэтому терминальные события
    (`done`, `error`) обязаны быть ещё и в журнале, а подписчик — уметь
    закончиться по журналу, а не только по сообщению из шины.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_REDIS_URL` (по умолчанию) — шина внутри
процесса, ровно как было: одна реплика, соседей нет. Непустой — Redis, и
тогда реплик может быть больше одной.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from finengine.config import settings

log = logging.getLogger(__name__)


class MessageBus(Protocol):
    """Канал — строка вида `run:<id>` или `station:<nonce>`."""

    async def publish(self, channel: str, message: dict) -> int: ...
    def subscribe(self, channel: str) -> "Подписка": ...
    async def close(self) -> None: ...


class Подписка:
    """Живой хвост канала. Открывается ДО того, как ждать сообщений.

    🔴 ОТКРЫТЬ И СЛУШАТЬ — РАЗНЫЕ МОМЕНТЫ, И РАЗДЕЛЕНЫ ОНИ НЕ ЗРЯ. Подписчику
    надо сперва открыть хвост, потом прочитать журнал (догон), и только потом
    слушать: иначе события, случившиеся между чтением журнала и подпиской,
    провалятся в щель. Обратный порядок даёт «пропало ровно одно событие в
    середине» — отказ, который ловится только глазами и не воспроизводится.
    """

    def __init__(self, очередь: asyncio.Queue, отписаться) -> None:
        self._q = очередь
        self._отписаться = отписаться
        self._закрыта = False

    async def get(self, timeout: float | None = None) -> dict | None:
        """Следующее сообщение. `None` — вышло время ожидания."""
        if timeout is None:
            return await self._q.get()
        try:
            return await asyncio.wait_for(self._q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        if not self._закрыта:
            self._закрыта = True
            self._отписаться(self._q)

    def __enter__(self) -> "Подписка":
        return self

    def __exit__(self, *_) -> None:
        self.close()


_MAXQ = 2000


class LocalBus:
    """Шина внутри процесса. Текущее поведение, оформленное интерфейсом.

    ⚠️ НЕ ЗАГЛУШКА. При одной реплике это правильный выбор: обращение к Redis
    по сети дороже словаря, а гарантий даёт не больше.
    """

    def __init__(self) -> None:
        self._каналы: dict[str, list[asyncio.Queue]] = {}

    async def publish(self, channel: str, message: dict) -> int:
        принято = 0
        for q in list(self._каналы.get(channel) or ()):
            try:
                q.put_nowait(message)
                принято += 1
            except asyncio.QueueFull:
                # Переполненный подписчик пропускает событие, остальные
                # получают. Ронять публикацию из-за одного медленного
                # потребителя нельзя: это остановило бы весь прогон.
                log.warning("шина: очередь подписчика канала %s переполнена", channel)
        return принято

    def subscribe(self, channel: str) -> Подписка:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAXQ)
        self._каналы.setdefault(channel, []).append(q)

        def _снять(очередь: asyncio.Queue) -> None:
            список = self._каналы.get(channel)
            if список is None:
                return
            if очередь in список:
                список.remove(очередь)
            if not список:
                self._каналы.pop(channel, None)

        return Подписка(q, _снять)

    async def close(self) -> None:
        self._каналы.clear()


class RedisBus:
    """Redis pub/sub. Сообщение едет JSON-ом.

    Одно соединение на процесс и ОДИН слушающий цикл: у Redis подписка живёт
    на соединении, и заводить его на каждого подписчика значило бы держать
    сотню сокетов вместо одного.
    """

    def __init__(self, url: str, prefix: str | None = None) -> None:
        import redis.asyncio as _redis  # noqa: PLC0415 — только эта ветка

        self._prefix = settings.redis_prefix if prefix is None else prefix
        self._r = _redis.Redis.from_url(url, decode_responses=True)
        self._pubsub = self._r.pubsub(ignore_subscribe_messages=True)
        self._каналы: dict[str, list[asyncio.Queue]] = {}
        self._цикл: asyncio.Task | None = None

    async def publish(self, channel: str, message: dict) -> int:
        return int(await self._r.publish(
            self._k(channel),
            json.dumps(message, ensure_ascii=False, default=str)) or 0)

    def _k(self, channel: str) -> str:
        """Имя канала в Redis. Приставка ставится ЗДЕСЬ и только здесь."""
        return f"{self._prefix}{channel}"

    def subscribe(self, channel: str) -> Подписка:
        # Дальше по коду `channel` — уже имя Redis: под ним лежит очередь в
        # `_каналы`, его же возвращает `get_message`, по нему идёт разбор.
        # Иначе пришлось бы срезать приставку в цикле чтения — лишний шов.
        channel = self._k(channel)
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAXQ)
        новый = channel not in self._каналы
        self._каналы.setdefault(channel, []).append(q)
        if новый:
            # Подписка на канал Redis — асинхронная, а `subscribe` синхронна
            # намеренно (см. докстринг `Подписка`: открыть хвост надо ДО чтения
            # журнала, и делать это через await значило бы дать щель).
            # Поэтому заводим задачу; она успевает подписаться раньше, чем
            # вызывающий дочитает журнал.
            asyncio.get_event_loop().create_task(self._подписаться(channel))

        def _снять(очередь: asyncio.Queue) -> None:
            список = self._каналы.get(channel)
            if список is None:
                return
            if очередь in список:
                список.remove(очередь)
            if not список:
                self._каналы.pop(channel, None)
                asyncio.get_event_loop().create_task(self._отписаться(channel))

        return Подписка(q, _снять)

    async def _подписаться(self, channel: str) -> None:
        # 🔴 ОТКАЗ ЗДЕСЬ НЕЛЬЗЯ ПРОСТО УРОНИТЬ. Задачу подписки никто не
        # await'ит (см. subscribe: открыть хвост надо синхронно), поэтому её
        # исключение умирает молча — а канал при этом УЖЕ лежит в `_каналы`,
        # и повторный subscribe считает его взведённым. Итог одноразового
        # отказа Redis в момент подписки — канал, глухой навсегда; для канала
        # тикетов (api/internal_tools) это значит вечный «running» у поллинга:
        # подписка там одна на процесс, второй попытки извне не будет.
        # Поэтому — повтор до успеха, пока канал кому-нибудь нужен.
        while channel in self._каналы:
            try:
                await self._pubsub.subscribe(channel)
                break
            except Exception as e:  # noqa: BLE001 — обрыв Redis не роняет процесс
                log.warning("шина: подписка на %s не удалась, повтор: %s", channel, e)
                await asyncio.sleep(1.0)
        else:
            return  # всех подписчиков сняли, пока Redis был недоступен
        # 🔴 ЦИКЛ ЧТЕНИЯ ПОДНИМАЕТСЯ ТОЛЬКО ПОСЛЕ ПЕРВОЙ УСПЕШНОЙ ПОДПИСКИ.
        # Заведённый раньше, он зовёт `get_message` на соединении без подписок,
        # и Redis отвечает «pubsub connection not set» — раз в секунду в лог, а
        # на каждый такой круг цикл засыпал, откладывая приём настоящих
        # сообщений. Поймано на стенде при первом запуске двух воркеров.
        # Повтор выше закрывает и второе следствие того же отказа: упади
        # подписка молча — цикл не поднялся бы вовсе.
        if self._цикл is None or self._цикл.done():
            self._цикл = asyncio.get_event_loop().create_task(self._слушать())

    async def _отписаться(self, channel: str) -> None:
        # Канал мог быть переподписан, пока задача ждала очереди событий.
        if channel not in self._каналы:
            await self._pubsub.unsubscribe(channel)

    async def _слушать(self) -> None:
        """Один цикл на процесс: читает Redis и разносит по локальным очередям."""
        while True:
            try:
                сообщение = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0)
            except Exception as e:  # noqa: BLE001 — обрыв Redis не роняет процесс
                log.warning("шина: чтение Redis прервалось: %s", e)
                await asyncio.sleep(1.0)
                continue
            if сообщение is None:
                await asyncio.sleep(0)
                continue
            канал = сообщение.get("channel") or ""
            try:
                данные = json.loads(сообщение.get("data") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for q in list(self._каналы.get(канал) or ()):
                try:
                    q.put_nowait(данные)
                except asyncio.QueueFull:
                    log.warning("шина: очередь подписчика канала %s переполнена", канал)

    async def close(self) -> None:
        if self._цикл is not None and not self._цикл.done():
            self._цикл.cancel()
        await self._pubsub.aclose()
        await self._r.aclose()


_cached: tuple[str, MessageBus] | None = None


def get_bus() -> MessageBus:
    """ПЕРЕКЛЮЧАТЕЛЬ: Redis — если настроен, иначе шина внутри процесса."""
    global _cached
    url = (settings.redis_url or "").strip()
    # В ключе синглтона и приставка: сменить контур, не сменив адрес, —
    # законный ход, а по одному адресу мы бы отдали объект со старой приставкой.
    ключ = f"{url}\n{settings.redis_prefix}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    шина: MessageBus = RedisBus(url) if url else LocalBus()
    # Адрес не печатаем — в нём может быть пароль.
    log.info("шина сообщений: %s", "Redis" if url else "внутри процесса")
    _cached = (ключ, шина)
    return шина


def reset_bus() -> None:
    """Забыть шину. Только для тестов и смены настроек — не боевой путь."""
    global _cached
    _cached = None


def проверить_готовность_к_репликам() -> None:
    """Не дать поднять несколько воркеров без общей шины.

    🔴 ЭТО ЛОВУШКА «ОДНО РЕШЕНИЕ В ДВУХ МЕСТАХ», И ОНА ТИХАЯ. Число воркеров
    задаётся в одном месте (`FINENGINE_WORKERS`), общая шина — в другом
    (`FINENGINE_REDIS_URL`). Поднять первое, забыв второе, — значит вернуть
    ровно тот разъезд, ради устранения которого всё и делалось: у каждого
    воркера своя шина внутри процесса, и ход рвётся между ними. Отказа при
    этом не будет: запросы просто станут отвечать «ход не зарегистрирован» и
    «unknown ticket», а подписки — обрываться на середине.

    Поэтому падаем на старте и говорим, что именно не так. Тихо разъехаться
    хуже, чем не запуститься: во втором случае это видно сразу.
    """
    import os  # noqa: PLC0415

    воркеров = int(os.environ.get("FINENGINE_WORKERS") or "1")
    if воркеров <= 1:
        return
    if (settings.redis_url or "").strip():
        log.info("реплик: %d, шина общая — разъезда не будет", воркеров)
        return
    raise RuntimeError(
        f"FINENGINE_WORKERS={воркеров}, но FINENGINE_REDIS_URL пуст. "
        "Без общей шины каждый воркер живёт своей: ход рвётся между ними — "
        "тулы отвечают «ход не зарегистрирован», поллинг «unknown ticket», "
        "подписка обрывается на середине. Задайте адрес Redis либо верните "
        "FINENGINE_WORKERS=1."
    )
