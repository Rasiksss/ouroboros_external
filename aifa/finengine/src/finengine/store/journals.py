"""Журналы: следы, аудит и расход модели. Файлы на томе или строки в базе.

ТРИ ЖУРНАЛА ОДНОЙ ФОРМЫ, И ЭТО НЕ СОВПАДЕНИЕ. Каждый — дописывание строки JSON
в `<вид>/<владелец>/<ключ>.jsonl` и чтение файла целиком:

  * `traces`     — полная лента событий хода (`observability.py`), читает
                   `/admin/trace/{session_id}`;
  * `audit`      — вызовы тулов: аргументы, исход, задержка (`audit/`);
  * `llm_traces` — расход модели по проекту, читает `/projects/{id}/usage` —
                   это ВИДИТ ПОЛЬЗОВАТЕЛЬ под строкой ввода.

Пока их было три модуля, каждый заводил свой каталог, свой `mkdir`, свой
разбор строк и свою обработку битой строки. Здесь одно место.

ЗАЧЕМ ПЕРЕЕЗД. `docs/rules/architecture.md` 3.1 (🔴 MUST): «Нет состояния на
файловой системе контейнера… Нет локальных лог-файлов»; 3.2: «журналы →
spool→S3». На реплике с read-only корневой ФС дописывание в файл не работает
вовсе, а на нескольких репликах лента одного хода разъезжается по репликам —
и `/admin/trace` показывает половину.

🔴 ПОЧЕМУ БАЗА, А НЕ S3, РАЗ ПРАВИЛО ГОВОРИТ «spool→S3». Все три читаются НЕ
целиком, а по ключу: лента одной сессии, аудит одного проекта, расход одного
проекта. S3 хорош, когда журнал пишут и потом увозят пачкой; у нас его тут же
и спрашивают, причём `llm_traces` — на каждом открытии проекта. Складывать
объект на строку — это тысячи мелких объектов и запрос на каждую; складывать
пачкой — значит писать свой буфер и терять хвост при перезапуске.

⚠️ `traces` — ТЯЖЁЛЫЙ, и это надо назвать вслух, а не спрятать. На стенде он
39 МБ против 1,2 МБ у двух других вместе: в него ложатся ПОЛНЫЕ полезные
нагрузки без урезания — так и задумано, отладка требует сырых байт. Сколько его
хранить — решение владельца, и здесь оно не принимается: своей политики
удаления этот модуль не заводит.

ПЕРЕКЛЮЧАТЕЛЬ. Пустой `FINENGINE_JOURNALS_DATABASE_URL` (по умолчанию) — файлы,
ровно как было. Непустой — база.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import (
    JSON, BigInteger, Column, Float, Index, MetaData, String, Table,
    select,
)

from finengine.config import settings
from finengine.store.engine import движок

log = logging.getLogger(__name__)

# Виды журналов. Имя вида — оно же имя каталога на томе: одна строка на обе
# реализации, иначе после переезда старые файлы искались бы не там.
TRACES = "traces"
AUDIT = "audit"
LLM_TRACES = "llm_traces"

_metadata = MetaData()

JOURNAL = Table(
    "journal", _metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),
    Column("owner_key", String(64), nullable=False),
    Column("key", String(128), nullable=False),
    # Время отдельной колонкой, хотя оно есть и внутри записи: по нему идёт
    # порядок чтения, а разбирать ради этого JSON каждой строки — расточительно.
    Column("ts", Float, nullable=False),
    Column("entry", JSON, nullable=False),
)
# Читают всегда по тройке (вид, владелец, ключ) — индекс ровно по ней.
Index("ix_journal_lookup", JOURNAL.c.kind, JOURNAL.c.owner_key, JOURNAL.c.key)


def safe_segment(s: str) -> str:
    """Кусок ключа. Одна функция на обе реализации.

    Сужаем набор символов: владелец и ключ приходят из запроса и не должны
    уводить путь за пределы каталога журнала. Для базы обход пути не страшен,
    но ключ обязан совпадать в обеих ветках — иначе после переезда лента не
    найдётся, а выглядеть это будет как «следов нет».
    """
    return "".join(c for c in s if c.isalnum() or c in ("-", "_")) or "_"


class JournalStore(Protocol):
    def append(self, kind: str, owner: str, key: str, entry: dict) -> None: ...
    def read(self, kind: str, owner: str, key: str) -> list[dict]: ...
    def keys(self, kind: str, owner: str) -> list[str]: ...


def _время(raw: object) -> float:
    """Отметка времени записи. Что угодно на входе — число на выходе.

    🔴 ЗДЕСЬ СТОЯЛО `float(значение.get("ts") or 0.0)`, И ЭТО ТЕРЯЛО ЗАПИСИ.
    У журналов прогонов `ts` — строка ISO (`"2026-08-15T12:46:58"`), а не число
    epoch: так их писали с самого начала. `float()` на такой строке бросает,
    запись не ложится, а сторож уровнем выше проглатывает отказ — как и должен,
    журнал не вправе уронить ход. Итог: тишина и потерянные записи.

    Поймано переездом: из четырёх журналов доехал ОДИН (сырьё станции, у него
    `ts` числом), а 411 записей извлечений, чата и разборов не легли ни одной.
    Живые записи теряло бы так же.

    Время нужно ТОЛЬКО для порядка и отбора; сама запись едет целиком в `entry`.
    Поэтому неразобранная отметка — не повод терять запись: ноль честнее потери.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    строка = str(raw).strip()
    if not строка:
        return 0.0
    try:
        return float(строка)
    except ValueError:
        pass
    from datetime import datetime  # noqa: PLC0415

    try:
        return datetime.fromisoformat(строка).timestamp()
    except ValueError:
        log.debug("журнал: отметка времени %r не разобрана — пишу 0", строка)
        return 0.0


class LocalJournalStore:
    """Файлы на томе: `<вид>/<владелец>/<ключ>.jsonl`.

    ⚠️ НЕ ЗАГЛУШКА: сегодняшнее боевое поведение и путь отката.
    """

    def _файл(self, kind: str, owner: str, key: str) -> Path:
        return Path(settings.data_dir) / kind / owner / f"{key}.jsonl"

    def append(self, kind: str, owner: str, key: str, entry: dict) -> None:
        путь = self._файл(kind, owner, key)
        путь.parent.mkdir(parents=True, exist_ok=True)
        with путь.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def read(self, kind: str, owner: str, key: str) -> list[dict]:
        путь = self._файл(kind, owner, key)
        if not путь.is_file():
            return []
        записи: list[dict] = []
        for строка in путь.read_text(encoding="utf-8").splitlines():
            if not строка.strip():
                continue
            # Битую строку пропускаем: журнал дописывается и может оборваться
            # на перезапуске, а лента из-за одной строки пропадать не должна.
            try:
                записи.append(json.loads(строка))
            except json.JSONDecodeError:
                continue
        return записи

    def keys(self, kind: str, owner: str) -> list[str]:
        каталог = Path(settings.data_dir) / kind / owner
        if not каталог.is_dir():
            return []
        return sorted(p.stem for p in каталог.glob("*.jsonl"))


class SqlJournalStore:
    """Строка на запись. Вид, владелец и ключ — в условии каждого чтения."""

    def __init__(self, url: str):
        # echo=False намеренно: в строке подключения пароль.
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        _metadata.create_all(self._engine)

    def append(self, kind: str, owner: str, key: str, entry: dict) -> None:
        # `default=str` — как у файловой ветки: в след кладут что угодно, вплоть
        # до объектов, и падать на сериализации журнал не имеет права.
        значение = json.loads(json.dumps(entry, ensure_ascii=False, default=str))
        with self._engine.begin() as conn:
            conn.execute(JOURNAL.insert().values(
                kind=kind, owner_key=owner, key=key,
                ts=_время(значение.get("ts")), entry=значение))

    def read(self, kind: str, owner: str, key: str) -> list[dict]:
        запрос = (select(JOURNAL.c.entry)
                  .where(JOURNAL.c.kind == kind, JOURNAL.c.owner_key == owner,
                         JOURNAL.c.key == key)
                  # По `id`, а не по `ts`: у двух событий одного хода время
                  # совпадает до миллисекунды, и порядок ленты поплыл бы.
                  .order_by(JOURNAL.c.id))
        with self._engine.begin() as conn:
            return [json.loads(з) if isinstance(з, str) else з
                    for (з,) in conn.execute(запрос)]

    def keys(self, kind: str, owner: str) -> list[str]:
        запрос = (select(JOURNAL.c.key).distinct()
                  .where(JOURNAL.c.kind == kind, JOURNAL.c.owner_key == owner))
        with self._engine.begin() as conn:
            return sorted(k for (k,) in conn.execute(запрос))


_cached: tuple[str, JournalStore] | None = None


def get_journal_store() -> JournalStore:
    """ПЕРЕКЛЮЧАТЕЛЬ: база — если настроена, иначе файлы, как было."""
    global _cached
    url = (settings.journals_database_url or "").strip()
    # Каталог данных — в ключе кэша: в тестах он подменяется.
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА. Она ставится ПОДКЛЮЧЕНИЮ (store/engine.py),
    # то есть живёт внутри движка: по одному адресу с двумя схемами кэш отдавал
    # бы хранилище, смотрящее в ЧУЖУЮ. Тот же урок уже куплен news/scenarios
    # первым прогоном тестов против настоящего Postgres.
    from finengine.store.engine import схема  # noqa: PLC0415 — разрыв цикла: engine не должен тянуть хранилища

    ключ = f"{url}|{settings.data_dir}|{схема()}"
    if _cached is not None and _cached[0] == ключ:
        return _cached[1]
    хранилище: JournalStore = SqlJournalStore(url) if url else LocalJournalStore()
    # Адрес не печатаем — в нём пароль.
    log.info("хранилище журналов: %s", "база" if url else "файлы на томе")
    _cached = (ключ, хранилище)
    return хранилище


def append(kind: str, *, owner: Any, key: Any, entry: dict) -> None:
    """Дописать запись. Никогда не бросает наружу — журнал не ломает ход.

    Молчать про отказ нельзя (тогда пропажу следов не с чем связать), но и
    ронять из-за него ход — тем более: пользователь потеряет ответ из-за
    недоступного журнала.
    """
    o, k = safe_segment(str(owner) if owner is not None else "anonymous"), safe_segment(str(key))
    try:
        get_journal_store().append(kind, o, k, entry)
    except Exception as e:  # noqa: BLE001 — журнал не имеет права уронить ход
        log.warning("журнал %s: запись %s/%s не легла: %s", kind, o, k, e)


def read(kind: str, *, owner: Any, key: Any) -> list[dict]:
    """Все записи по ключу, в порядке появления. Нет — пустой список."""
    o, k = safe_segment(str(owner) if owner is not None else "anonymous"), safe_segment(str(key))
    try:
        return get_journal_store().read(kind, o, k)
    except Exception as e:  # noqa: BLE001 — см. `append`
        log.warning("журнал %s: чтение %s/%s не удалось: %s", kind, o, k, e)
        return []


def keys(kind: str, *, owner: Any) -> list[str]:
    """Все ключи вида у владельца. Нужен обходу расхода по всем проектам."""
    o = safe_segment(str(owner) if owner is not None else "anonymous")
    try:
        return get_journal_store().keys(kind, o)
    except Exception as e:  # noqa: BLE001 — см. `append`
        log.warning("журнал %s: перечень ключей %s не удался: %s", kind, o, e)
        return []
