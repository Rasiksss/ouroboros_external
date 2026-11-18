"""Общий кэш координации: память процесса или Redis. Ключ, значение, срок.

ЗАЧЕМ. `docs/rules/architecture.md` 3.2 (🔴 MUST): «Stateless-реплики;
сессии/состояние — в общих хранилищах… In-process синглтоны/счётчики/локи,
разъезжающиеся на `>1` реплике — баги. Сессии/SSE-состояние → Redis».

По пакету было восемь таких мест. Пять из них — обычные словари «ключ →
значение + срок»: они и переезжают сюда. Три оставшихся держат ЖИВЫЕ
asyncio-объекты и механически не переносятся; почему — сказано ниже, чтобы
следующий не искал.

🔴 ЧТО СЮДА НЕ ПЕРЕЕХАЛО И ПОЧЕМУ (не забыто, а отложено осознанно):

  * `run_orchestrator._runs` — реестр живых прогонов. Держит `asyncio.Queue`
    наблюдателей и `asyncio.Task` самого прогона. Очередь и задача существуют
    ВНУТРИ одного цикла событий: сериализовать их нельзя, а значит перенос —
    это не смена хранилища, а перепроектирование раздачи: журнал прогона в
    общее хранилище, раздача через pub/sub, у каждой реплики свои локальные
    очереди на подписке, отмена — сигналом в канал;
  * `station_progress_ingest._bus` — то же самое: списки очередей на нонс;
  * `internal_tools._tickets` — в значении лежит живой `task`.

⚠️ ПОКА РЕПЛИКА ОДНА, ЭТО НЕ ДЕФЕКТ, А ЦЕНА. `--workers 1` в Dockerfile
бэкенда — несущий именно из-за этих трёх. Снимать его, не переделав раздачу,
нельзя: поток SSE пойдёт в одну реплику, а событие прогона придёт в другую, и
пользователь не увидит ответа. Это ровно тот класс отказа, который мы чинили
весь день, — поэтому половинчатый перенос хуже честного «ещё не сделано».

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_REDIS_URL` (по умолчанию) — память процесса,
ровно как было. Непустой — Redis. Момент выбирает владелец; код ничего не
включает сам, как у диалогов, учёток, документов, объектов и новостей.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from finengine.config import settings

log = logging.getLogger(__name__)


class KeyValueCache(Protocol):
    """Ключ → значение со сроком. Значение обязано быть JSON-совместимым."""

    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, ttl_sec: float) -> None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str = "") -> list[str]: ...


class LocalCache:
    """Память процесса. Текущее поведение, оформленное интерфейсом.

    ⚠️ НЕ ЗАГЛУШКА. При одной реплике это правильный выбор: обращение к Redis
    по сети дороже словаря, а гарантий даёт не больше. Ветка боевая, и покрыта
    теми же тестами, что и Redis-овая.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}

    def _sweep(self, now: float) -> None:
        # Подметаем на каждом обращении, а не по таймеру: записей десятки, а
        # фоновой задаче пришлось бы жить своим жизненным циклом.
        for k in [k for k, (_, exp) in self._store.items() if exp < now]:
            self._store.pop(k, None)

    def get(self, key: str) -> Any | None:
        now = time.time()
        self._sweep(now)
        пара = self._store.get(key)
        # ⚠️ Вторая проверка срока НЕ НЕСУЩАЯ, и честнее сказать, чем сделать
        # вид: `_sweep` выше уже убрал просроченное, поэтому мутация «убрать
        # эту проверку» тест не краснит. Она закрывает окно в микросекунды —
        # запись, истёкшую между подметанием и чтением, — и проверить это
        # тестом нечем.
        if пара is None or пара[1] < now:
            return None
        return пара[0]

    def set(self, key: str, value: Any, ttl_sec: float) -> None:
        now = time.time()
        self._sweep(now)
        self._store[key] = (value, now + ttl_sec)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def keys(self, prefix: str = "") -> list[str]:
        now = time.time()
        self._sweep(now)
        return sorted(k for k in self._store if k.startswith(prefix))


class RedisCache:
    """Redis. Значение едет JSON-ом, срок — родным TTL ключа.

    Срок отдаём Redis, а не храним рядом со значением: иначе истёкшие записи
    занимали бы память до следующего обращения, а на нескольких репликах ещё и
    подметались бы по-разному.
    """

    def __init__(self, url: str, prefix: str | None = None):
        import redis  # noqa: PLC0415 — тяжёлая зависимость, только для этой ветки

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._prefix = settings.redis_prefix if prefix is None else prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        сырьё = self._r.get(self._k(key))
        return json.loads(сырьё) if сырьё is not None else None

    def set(self, key: str, value: Any, ttl_sec: float) -> None:
        # px, а не ex: сроки у нас дробные (0.5 с в тестах), а `ex` целочислен
        # и молча округлил бы их до нуля — запись исчезала бы сразу.
        self._r.set(self._k(key), json.dumps(value, ensure_ascii=False),
                    px=max(1, int(ttl_sec * 1000)))

    def delete(self, key: str) -> None:
        self._r.delete(self._k(key))

    def keys(self, prefix: str = "") -> list[str]:
        срез = len(self._prefix)
        return sorted(k[срез:] for k in self._r.scan_iter(f"{self._prefix}{prefix}*"))


_cached: tuple[str, KeyValueCache] | None = None


def get_cache() -> KeyValueCache:
    """ПЕРЕКЛЮЧАТЕЛЬ: Redis — если настроен, иначе память процесса."""
    global _cached
    url = (settings.redis_url or "").strip()
    # В ключе синглтона и приставка: сменить контур, не сменив адрес, —
    # законный ход, а по одному адресу мы бы отдали объект со старой приставкой.
    ключ = f"{url}\n{settings.redis_prefix}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    кэш: KeyValueCache = RedisCache(url) if url else LocalCache()
    # Адрес не печатаем — в нём может быть пароль.
    log.info("кэш координации: %s", "Redis" if url else "память процесса")
    _cached = (ключ, кэш)
    return кэш
