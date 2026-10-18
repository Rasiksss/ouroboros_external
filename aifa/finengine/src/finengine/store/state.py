"""Именованное состояние: (вид, владелец, ключ) → документ JSON.

🔴 ЗАЧЕМ ЭТОТ МОДУЛЬ, А НЕ ЕЩЁ ОДИН СВОЙ. Переезжая с тома, я написал подряд
три хранилища одной формы — сессии, артефакты, журналы, — и на четвёртом
(списки наблюдения, личная методика, состояние пользователя) стало видно:
это одна вещь, и городить её пятый раз означало бы завести ровно тот дефект,
ради ухода от которого переезд и делается. Поэтому здесь общий механизм, а
`store/sessions.py` переведён на него и остался тонкой обёрткой со своим
именем — оно нужно, чтобы граница владельца была видна в подписи вызова.

ЧТО СЮДА НЕ ОТНОСИТСЯ, И ЭТО ОСОЗНАННО:

  * `store/journals.py` — там ДОПИСЫВАНИЕ в ленту, а не документ: у записи нет
    ключа, и порядок появления несущий;
  * `store/artifacts.py` — там пара «карточка + нагрузка» и перечень по
    времени: сведение к одному документу заставило бы тянуть мегабайты
    нагрузки ради списка;
  * `store/blobs.py` — там байты (PDF, книги Excel), а не JSON.

ЗАЧЕМ ВООБЩЕ. `docs/rules/architecture.md` 3.1 (🔴 MUST): «Нет состояния на
файловой системе контейнера»; 3.2: запрос не должен зависеть от реплики.

🔴 ГРАНИЦА ВЛАДЕЛЬЦА. На томе она физическая — каталог на пользователя; в базе
её держит условие `owner_key` в запросе. Разбор цены ошибки — в шапке
`store/sessions.py`. Здесь оно в условии КАЖДОГО обращения, включая запись.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_STATE_DATABASE_URL` (по умолчанию) — файлы,
ровно как было. Непустой — база.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import (
    JSON, Column, MetaData, String, Table, delete, select,
)

from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)

# Виды состояния. Имя вида — оно же кусок пути на томе.
SESSION = "sessions"
WATCHLIST = "watchlist"
USER_STATE = "user_state"
METHODOLOGY = "methodology"
SOURCE_META = "source_meta"
# Ходы сессии экрана через ouroboros: чат и корень каждого (`assistant/session_turns.py`, CONTRACTS.md §2.6).
CHAT_SESSION = "chat_session"
# Контекст хода гейтвей-петли: владелец, проект, прогон, признак
# дословности. Владельца в ключе НЕТ намеренно — ключ и есть свежий
# uuid хода, а сам владелец лежит ВНУТРИ записи и оттуда берётся для
# owner-гейта тулов. Поставить его в ключ значило бы требовать его до
# того, как он стал известен.
GATEWAY_SESSION = "gateway_session"

_metadata = MetaData()

STATE = Table(
    "state", _metadata,
    # Ключ составной. Причина — из живого урока сессий: при ключе из одного
    # идентификатора база отвечала на чужую запись `IntegrityError` вместо
    # изоляции, тогда как на томе это просто два файла в разных каталогах.
    Column("kind", String(32), primary_key=True),
    Column("owner_key", String(64), primary_key=True),
    Column("key", String(128), primary_key=True),
    Column("doc", JSON, nullable=False),
)


def safe_segment(s: str) -> str:
    """Кусок ключа. Одна функция на обе реализации — иначе после переезда
    состояние не найдётся, а выглядеть это будет как «настройки сбросились».
    """
    return "".join(c for c in str(s) if c.isalnum() or c in ("-", "_")) or "_"


class StateStore(Protocol):
    def get(self, kind: str, owner: str, key: str) -> dict | None: ...
    def put(self, kind: str, owner: str, key: str, doc: dict) -> None: ...
    def delete(self, kind: str, owner: str, key: str) -> bool: ...
    def keys(self, kind: str, owner: str) -> list[str]: ...
    def mutate(self, kind: str, owner: str, key: str, изменить) -> dict: ...


class LocalStateStore:
    """Файлы на томе: `<вид>/<владелец>/<ключ>.json`.

    ⚠️ НЕ ЗАГЛУШКА: сегодняшнее боевое поведение и путь отката.
    """

    def _файл(self, kind: str, owner: str, key: str) -> Path:
        return Path(settings.data_dir) / kind / owner / f"{key}.json"

    def get(self, kind: str, owner: str, key: str) -> dict | None:
        путь = self._файл(kind, owner, key)
        if not путь.is_file():
            return None
        return json.loads(путь.read_text(encoding="utf-8"))

    def put(self, kind: str, owner: str, key: str, doc: dict) -> None:
        путь = self._файл(kind, owner, key)
        путь.parent.mkdir(parents=True, exist_ok=True)
        # Через временный файл и `replace`: читатель никогда не увидит
        # наполовину записанный документ.
        врем = путь.with_suffix(".tmp")
        врем.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        врем.replace(путь)

    def delete(self, kind: str, owner: str, key: str) -> bool:
        путь = self._файл(kind, owner, key)
        if not путь.is_file():
            return False
        путь.unlink()
        return True

    def keys(self, kind: str, owner: str) -> list[str]:
        каталог = Path(settings.data_dir) / kind / owner
        if not каталог.is_dir():
            return []
        return sorted(p.stem for p in каталог.glob("*.json"))

    def mutate(self, kind: str, owner: str, key: str, изменить) -> dict:
        """«Прочитать → изменить → записать» под файловой блокировкой.

        Блокировка ФАЙЛОВАЯ (flock), а не `threading.Lock`: воркеры — отдельные
        ПРОЦЕССЫ, и внутрипроцессный замок их бы не удержал.

        Замок — на ОТДЕЛЬНОМ файле рядом, а не на самом документе: документ
        переписывается через `replace`, то есть подменяется инодой, и захваченная
        на нём блокировка удерживала бы уже несуществующий объект.
        """
        import fcntl  # noqa: PLC0415 — только эта ветка и только на POSIX

        путь = self._файл(kind, owner, key)
        путь.parent.mkdir(parents=True, exist_ok=True)
        замок = путь.with_suffix(".lock")
        with open(замок, "a+") as fh:  # noqa: PTH123 — flock нужен дескриптор
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                новый = изменить(self.get(kind, owner, key))
                self.put(kind, owner, key, новый)
                return новый
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class SqlStateStore:
    """Строка на документ. Вид, владелец и ключ — в условии каждого обращения."""

    def __init__(self, url: str):
        # echo=False намеренно: в строке подключения пароль.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        _metadata.create_all(self._engine)

    def _где(self, kind: str, owner: str, key: str | None = None):
        условия = [STATE.c.kind == kind, STATE.c.owner_key == owner]
        if key is not None:
            условия.append(STATE.c.key == key)
        return условия

    def get(self, kind: str, owner: str, key: str) -> dict | None:
        with self._engine.begin() as conn:
            строка = conn.execute(
                select(STATE.c.doc).where(*self._где(kind, owner, key))).first()
        if not строка:
            return None
        значение: Any = строка[0]
        return json.loads(значение) if isinstance(значение, str) else значение

    def put(self, kind: str, owner: str, key: str, doc: dict) -> None:
        with self._engine.begin() as conn:
            # «Снести и вставить» вместо диалектного upsert: у Postgres есть
            # ON CONFLICT, у SQLite старых версий нет, а гонку закрывает
            # транзакция. Условие включает владельца и здесь — иначе чужой
            # ход, знающий ключ, переписал бы чужой документ.
            conn.execute(delete(STATE).where(*self._где(kind, owner, key)))
            conn.execute(STATE.insert().values(
                kind=kind, owner_key=owner, key=key, doc=doc))

    def delete(self, kind: str, owner: str, key: str) -> bool:
        with self._engine.begin() as conn:
            итог = conn.execute(delete(STATE).where(*self._где(kind, owner, key)))
        return bool(итог.rowcount)

    def keys(self, kind: str, owner: str) -> list[str]:
        запрос = select(STATE.c.key).distinct().where(*self._где(kind, owner))
        with self._engine.begin() as conn:
            return sorted(k for (k,) in conn.execute(запрос))

    def mutate(self, kind: str, owner: str, key: str, изменить) -> dict:
        """«Прочитать → изменить → записать» ОДНОЙ транзакцией.

        🔴 РАДИ ЭТОГО И ДЕЛАЛСЯ ПЕРЕЕЗД. Без взаимного исключения две правки,
        пришедшие одновременно, читают один и тот же документ, и вторая пишет
        поверх первой: правка пропадает БЕЗ ошибки — худший вид отказа, он
        неотличим от «не сработало». На томе это закрывалось файловым замком, и
        в его описании прямо стояло: «снимется переездом в Postgres — там это
        транзакция». Вот она.

        ⚠️ НА SQLITE `FOR UPDATE` НЕ ПОДДЕРЖИВАЕТСЯ и молча игнорируется —
        сериализацию там даёт сама транзакция. Сказано вслух, потому что тест
        на SQLite не отличит одно от другого и «зелёный» ничего не докажет.
        """
        with self._engine.begin() as conn:
            запрос = select(STATE.c.doc).where(*self._где(kind, owner, key))
            if conn.dialect.name == "postgresql":
                запрос = запрос.with_for_update()
            строка = conn.execute(запрос).first()
            текущий = None
            if строка:
                значение: Any = строка[0]
                текущий = json.loads(значение) if isinstance(значение, str) else значение
            новый = изменить(текущий)
            conn.execute(delete(STATE).where(*self._где(kind, owner, key)))
            conn.execute(STATE.insert().values(
                kind=kind, owner_key=owner, key=key, doc=новый))
            return новый


_cached: tuple[str, StateStore] | None = None


def get_state_store() -> StateStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: база — если настроена, иначе файлы, как было."""
    global _cached
    url = (settings.state_database_url or "").strip()
    # Каталог данных — в ключе кэша: в тестах он подменяется.
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА. Она ставится ПОДКЛЮЧЕНИЮ (store/engine.py),
    # то есть живёт внутри движка: по одному адресу с двумя схемами кэш отдавал
    # бы хранилище, смотрящее в ЧУЖУЮ. Тот же урок уже куплен news/scenarios
    # первым прогоном тестов против настоящего Postgres.
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть хранилища

    ключ = f"{url}|{settings.data_dir}|{схема()}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    хранилище: StateStore = SqlStateStore(url) if url else LocalStateStore()
    # Адрес не печатаем — в нём пароль.
    log.info("хранилище состояния: %s", "база" if url else "файлы на томе")
    _cached = (ключ, хранилище)
    return хранилище


def get(kind: str, *, owner: Any, key: Any) -> dict | None:
    return get_state_store().get(kind, safe_segment(owner), safe_segment(key))


def put(kind: str, *, owner: Any, key: Any, doc: dict) -> None:
    get_state_store().put(kind, safe_segment(owner), safe_segment(key), doc)


def delete_one(kind: str, *, owner: Any, key: Any) -> bool:
    return get_state_store().delete(kind, safe_segment(owner), safe_segment(key))


def keys(kind: str, *, owner: Any) -> list[str]:
    return get_state_store().keys(kind, safe_segment(owner))


def mutate(kind: str, *, owner: Any, key: Any, изменить) -> dict:
    """Прочитать, изменить, записать — не теряя параллельную правку.

    `изменить(текущий_или_None) -> новый`. Функция может позваться внутри
    транзакции, поэтому обязана быть быстрой и без обращений наружу.
    """
    return get_state_store().mutate(kind, safe_segment(owner), safe_segment(key), изменить)
