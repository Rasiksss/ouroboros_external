"""Хранилище новостного конвейера. SQLAlchemy Core — SQLite или Postgres.

🔴 ПЕРЕПИСАНО 25.08.2026 (решение владельца: «делай так, чтобы соответствовало
правилам разработки»). Было — голый `sqlite3` с `PRAGMA`, `sqlite_master`,
`executescript`, `INSERT OR IGNORE` и `PRAGMA table_info`. Всё это есть ТОЛЬКО
у SQLite: модуль не запускался на Postgres в принципе, то есть
`docs/rules/architecture.md` 2.1 (🔴 MUST) «Postgres — единственная БД…
признак регрессии: файловые БД» было невыполнимо технически.

ГДЕ ЛЕЖИТ. По умолчанию — тот же файл `<data_dir>/news.db`, и SQLAlchemy
читает его как есть: схема объявлена так, чтобы совпасть с накопленной
миграциями и ALTER-ами. Непустой `FINENGINE_NEWS_DATABASE_URL` переводит на
базу; переезд — `python -m finengine.store.migrate_news`.

Таблицы:
  sources         каталог из sources.yaml (зеркалится ради join'ов)
  raw_items       всё собранное (после нормализации, до дедупа)
  digest_items    классифицированное и ранжированное, попавшее в выпуск
  digest_runs     сведения о прогоне (старт, финиш, стоимость, владелец, разбор)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Column, Float, Integer, MetaData, String, Table, Text, and_, inspect, select, text as sa_text, update as sa_update,
)

from finengine.config import settings
from finengine.skills.news.models import (
    DigestItem,
    NewsItem,
)
from finengine.store.engine import движок, схема

log = logging.getLogger(__name__)

_metadata = MetaData()

# Схема повторяет накопленную миграцией v1 плюс ALTER-ы `user_id` и `diag`:
# имена, типы и умолчания совпадают, поэтому существующий файл читается как
# есть, без переноса.
SOURCES = Table(
    "sources", _metadata,
    Column("source_id", String(128), primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("region", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("weight", Float, nullable=False, default=0.5),
    Column("active", Integer, nullable=False, default=1),
    Column("last_fetched", Float),
)

RAW_ITEMS = Table(
    "raw_items", _metadata,
    Column("raw_hash", String(128), primary_key=True),
    Column("source_id", String(128), nullable=False),
    Column("title", Text, nullable=False),
    Column("snippet", Text),
    Column("url", Text, nullable=False),
    Column("published_at", Float, nullable=False, index=True),
    Column("fetched_at", Float, nullable=False),
    Column("matched_json", Text),
)

DIGEST_RUNS = Table(
    "digest_runs", _metadata,
    Column("run_id", String(128), primary_key=True),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float),
    Column("profile", Text, nullable=False),
    Column("items_raw", Integer),
    Column("items_kept", Integer),
    Column("llm_in", Integer),
    Column("llm_out", Integer),
    Column("cost_usd", Float),
    Column("error", Text),
    # Добавлены ALTER-ом на живой базе — здесь объявлены наравне с остальными.
    Column("user_id", Integer),
    Column("diag", Text),
)

DIGEST_ITEMS = Table(
    "digest_items", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(128), nullable=False, index=True),
    Column("raw_hash", String(128), nullable=False),
    Column("is_relevant", Integer, nullable=False),
    Column("category", Text),
    Column("region", Text),
    Column("importance", Float),
    Column("final_score", Float),
    Column("in_long_list", Integer, nullable=False, default=0),
    Column("watchlist_match", Text),
    Column("one_sentence", Text),
    Column("why_important", Text),
)


def db_path() -> Path:
    """Путь СТАРОГО файла SQLite — теперь он нужен только переезду.

    Боевого файлового пути нет с 26.08.2026; функцию зовёт migrate_news,
    читающий накопленное. (Прежний докстринг оправдывал её резервными
    копиями — бэкапы её не используют, сверено ревизией 27.08.)
    """
    return settings.data_dir / "news.db"


# Кэш движка — ПО АДРЕСУ, а не «один на процесс».
#
# 🔴 ПОЧЕМУ ТАК (поймано прогоном 25.08). Прежний код открывал соединение по
# `db_path()` НА КАЖДЫЙ вызов, поэтому смена `settings.data_dir` немедленно
# переключала базу. Глобальный кэш это сломал: движок оставался от первого
# обращения, и обращение к другому каталогу молча шло в старую базу. Первыми
# это увидели тесты (каждый со своим tmp_path), но цена не тестовая: так же
# ведёт себя любая правка настроек на живом процессе.
_engines: dict[str, object] = {}


def engine():
    """Движок хранилища. Адрес ОБЯЗАТЕЛЕН, запасного пути нет."""
    url = (settings.news_database_url or "").strip()
    if not url:
        # 🔴 ФАЙЛОВЫЙ ЗАПАСНОЙ ПУТЬ СНЕСЁН 26.08.2026: здесь открывался
        # `sqlite:///news.db`. Пока он был, ненастроенный контур работал «как
        # будто получилось» — писал в файл, которого никто не читает.
        raise RuntimeError(
            "база новостей не настроена: пуст FINENGINE_NEWS_DATABASE_URL. "
            "Файлового запасного пути больше нет (снесён 26.08.2026)")
    # 🔴 В КЛЮЧЕ И СХЕМА КОНТУРА, А НЕ ТОЛЬКО АДРЕС. Схема ставится
    # ПОДКЛЮЧЕНИЮ (`store/engine.py`), то есть живёт внутри движка: по одному
    # адресу с двумя схемами кэш отдавал бы движок, смотрящий в ЧУЖУЮ. Поймано
    # первым же прогоном тестов против настоящего Postgres — «no schema has
    # been selected to create in» на схеме, которую предыдущий тест уже снёс.
    ключ = f"{url}\n{схема()}"
    дв = _engines.get(ключ)
    if дв is None:
        # echo=False намеренно: в строке подключения пароль.
        дв = движок(url, pool_pre_ping=True, future=True, echo=False)
        _engines[ключ] = дв
    return дв


def _upsert(conn, таблица, ключ_имя: str, ключ, значения: dict) -> None:
    """Вставить или обновить. Диалект решает как.

    У Postgres есть `ON CONFLICT`, у SQLite он появился поздно; путь
    «посмотреть и решить» работает на обоих, а гонку закрывает транзакция.
    """
    стлб = таблица.c[ключ_имя]
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        conn.execute(pg_insert(таблица).values(**значения).on_conflict_do_update(
            index_elements=[стлб],
            set_={k: v for k, v in значения.items() if k != ключ_имя}))
        return
    есть = conn.execute(select(стлб).where(стлб == ключ)).first()
    if есть:
        conn.execute(таблица.update().where(стлб == ключ).values(
            **{k: v for k, v in значения.items() if k != ключ_имя}))
    else:
        conn.execute(таблица.insert().values(**значения))


def _insert_ignore(conn, таблица, ключ_имя: str, ключ, значения: dict) -> bool:
    """Вставить, если такого ключа нет. True — вставили, False — уже был.

    Заменяет `INSERT OR IGNORE` (только SQLite). Возврат важен: на нём стоит
    подсчёт «сколько новых новостей собрали».
    """
    стлб = таблица.c[ключ_имя]
    if conn.execute(select(стлб).where(стлб == ключ)).first():
        return False
    conn.execute(таблица.insert().values(**значения))
    return True


def init_schema() -> None:
    """Идемпотентно, зовётся на каждом старте бэкенда.

    ⚠️ БЕЗ `sqlite_master`, `executescript` и `PRAGMA table_info` — их нет у
    Postgres. Состав колонок объявлен таблицами выше; недостающие в уже
    развёрнутой базе дописываются по инспектору. Колонки `user_id` и `diag`
    добавлялись ALTER-ом на живой базе, и старые файлы приходят без них.
    """
    дв = engine()
    _metadata.create_all(дв)
    инспектор = inspect(дв)
    если_есть = {c["name"] for c in инспектор.get_columns("digest_runs")}
    with дв.begin() as conn:
        for имя, тип in (("user_id", "INTEGER"), ("diag", "TEXT")):
            if имя not in если_есть:
                conn.execute(sa_text(f"ALTER TABLE digest_runs ADD COLUMN {имя} {тип}"))
                log.info("news.db: добавлена колонка %s", имя)


def upsert_source(
    source_id: str, display_name: str, source_type: str, region: str,
    url: str, weight: float, active: bool = True,
) -> None:
    with engine().begin() as conn:
        _upsert(conn, SOURCES, "source_id", source_id, {
            "source_id": source_id, "display_name": display_name,
            "source_type": source_type, "region": region, "url": url,
            "weight": weight, "active": 1 if active else 0})


def upsert_raw_item(item: NewsItem) -> bool:
    """Returns True if this was a new insert, False if duplicate (raw_hash collision)."""
    with engine().begin() as conn:
        return _insert_ignore(conn, RAW_ITEMS, "raw_hash", item.raw_hash, {
            "raw_hash": item.raw_hash, "source_id": item.source_id,
            "title": item.title, "snippet": item.snippet, "url": item.url,
            "published_at": item.published_at.timestamp(),
            "fetched_at": datetime.now(timezone.utc).timestamp(),
            "matched_json": json.dumps(
                {"entities": item.matched_entities, "topics": item.matched_topics},
                ensure_ascii=False)})


def set_matched(raw_hash: str, entities: list[str], topics: list[str]) -> None:
    """Save rule-filter annotations back to the DB row.

    raw_item is upserted at fetch time (before annotate), so matched_json
    is empty there. Pipeline calls this after annotate to backfill — keeps
    `matched_json` queryable for debugging filter coverage via SQL.
    """
    with engine().begin() as conn:
        conn.execute(sa_update(RAW_ITEMS).where(RAW_ITEMS.c.raw_hash == raw_hash)
                     .values(matched_json=json.dumps(
                         {"entities": entities, "topics": topics}, ensure_ascii=False)))


_СЫРЬЁ_С_ИСТОЧНИКОМ = None


def _сырьё_с_источником():
    """Колонки `raw_items` плюс сведения об источнике. Одно место на два запроса.

    Раньше этот SELECT был написан дважды почти дословно; разойдись они — один
    из потребителей молча остался бы без `source_name` или `weight`.
    """
    return (select(
        *RAW_ITEMS.c,
        SOURCES.c.display_name.label("source_name"),
        SOURCES.c.source_type,
        SOURCES.c.region.label("source_region"),
        SOURCES.c.weight,
    ).select_from(RAW_ITEMS.join(SOURCES, SOURCES.c.source_id == RAW_ITEMS.c.source_id)))


def recent_items_from_sources(
    source_ids: list[str], *, max_age_days: float, limit: int = 3,
) -> list[dict]:
    """Latest items from any of the given source_ids within `max_age_days`.
    Used by render to keep the regulator section always populated — if
    none of e.g. cbr_press/cbr_news/cbr_events fired today, fall back to
    their freshest item this week."""
    if not source_ids:
        return []
    since_ts = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    with engine().begin() as conn:
        rows = conn.execute(
            _сырьё_с_источником()
            .where(and_(RAW_ITEMS.c.source_id.in_(source_ids),
                        RAW_ITEMS.c.published_at >= since_ts))
            .order_by(RAW_ITEMS.c.published_at.desc())
            .limit(limit)).mappings().all()
        return [dict(r) for r in rows]


def fetch_unprocessed_since(since_ts: float) -> list[dict]:
    """All raw items published >= since_ts. Used by pipeline as the
    candidate pool for a digest run."""
    with engine().begin() as conn:
        rows = conn.execute(
            _сырьё_с_источником()
            .where(RAW_ITEMS.c.published_at >= since_ts)
            .order_by(RAW_ITEMS.c.published_at.desc())).mappings().all()
        return [dict(r) for r in rows]


def start_run(run_id: str, profile: str, user_id: int | None = None) -> None:
    """`user_id=None` — прогон планировщика: его выпуск никто не читает (он
    только пишется в лог и греет кэш сырья), поэтому в персональный дедуп он
    не попадает."""
    with engine().begin() as conn:
        conn.execute(DIGEST_RUNS.insert().values(
            run_id=run_id, started_at=datetime.now(timezone.utc).timestamp(),
            profile=profile, user_id=user_id))


# ⚠️ `_ensure_user_column` и `_ensure_diag_column` УБРАНЫ. Они дописывали
# колонки `user_id` и `diag` на живой базе через `PRAGMA table_info` — а его нет
# у Postgres, и зов на каждом запросе стоил лишнего обращения. Обе колонки
# теперь объявлены в `DIGEST_RUNS`, а дописывание старых баз делает `init_schema`
# один раз на старте, по инспектору SQLAlchemy.
#
# Причины, ради которых колонки заводили, никуда не делись и записаны там, где
# ими пользуются: отбор выпуска по владельцу (`latest_digest_run`) и
# межпрогонный дедуп (`recent_long_list_hashes`).

def finish_run(
    run_id: str, *, items_raw: int, items_kept: int,
    llm_in: int, llm_out: int, cost_usd: float, error: str | None = None,
    diag: dict | None = None,
) -> None:
    """`diag` — точки мониторинга прогона: модель, здоровье источников, время
    по стадиям. Кладём JSON-строкой в одну колонку: набор полей будет меняться
    по мере разбора, и заводить под каждое своё поле — множить миграции."""
    with engine().begin() as conn:
        conn.execute(sa_update(DIGEST_RUNS).where(DIGEST_RUNS.c.run_id == run_id).values(
            finished_at=datetime.now(timezone.utc).timestamp(),
            items_raw=items_raw, items_kept=items_kept,
            llm_in=llm_in, llm_out=llm_out, cost_usd=cost_usd, error=error,
            diag=json.dumps(diag, ensure_ascii=False) if diag else None))


def insert_digest_items(run_id: str, items: list[DigestItem]) -> None:
    if not items:
        return
    строки = [{
        "run_id": run_id,
        "raw_hash": d.classified.item.raw_hash,
        "is_relevant": 1 if d.classified.is_relevant else 0,
        "category": d.classified.category,
        "region": d.classified.region,
        "importance": d.classified.importance,
        "final_score": d.final_score,
        "in_long_list": 1 if d.in_long_list else 0,
        "watchlist_match": d.watchlist_match,
        "one_sentence": d.classified.one_sentence_ru,
        "why_important": d.classified.why_important,
    } for d in items]
    with engine().begin() as conn:
        conn.execute(DIGEST_ITEMS.insert(), строки)


def latest_digest_run(profile: str = "ir_market_news",
                      user_id: int | None = None) -> dict | None:
    """Most recent finished run for a profile. Used by on-demand UI:
    if there's a recent run (<24h old) return it directly, no live work.

    🔴 ОТБОР ПО ВЛАДЕЛЬЦУ (21.08.2026). Прежняя редакция брала последний
    удачный прогон КОГО УГОДНО: пользователь открывал «Дайджест» и получал
    чужой выпуск — собранный по чужому окну сбора, чужим группам источников и
    чужому фокусу. При трёх владельцах на проде и ежедневном прогоне
    планировщика (`user_id IS NULL`, его выпуск не читает никто) свой выпуск
    пользователь видел только сразу после собственного запуска.

    Тот же класс, что закрывали для межпрогонного дедупа: у прогона просто не
    было владельца, и код честно отдавал «последний».

    ⚠️ Сравнение через `IS`, а не `= `: в SQL `NULL = NULL` ложно, и выборка
    прогонов планировщика молча опустела бы.
    """
    # Сравнение владельца через `is_`, когда его нет: в СЫРОМ SQL `NULL = NULL`
    # ложно, и выборка прогонов планировщика молча опустела бы.
    #
    # ⚠️ ЗДЕСЬ ЭТО НЕ НЕСУЩЕЕ, и честнее сказать, чем сделать вид: SQLAlchemy
    # сам компилирует `== None` в `IS NULL`, поэтому мутация «заменить на ==»
    # тест НЕ КРАСНИТ. Ветка оставлена как заявление о намерении — на случай,
    # если условие однажды перепишут сырым SQL, где правило снова заработает.
    условие_владельца = (DIGEST_RUNS.c.user_id.is_(None) if user_id is None
                         else DIGEST_RUNS.c.user_id == user_id)
    with engine().begin() as conn:
        row = conn.execute(
            select(DIGEST_RUNS).where(and_(
                DIGEST_RUNS.c.profile == profile,
                DIGEST_RUNS.c.finished_at.isnot(None),
                DIGEST_RUNS.c.error.is_(None),
                условие_владельца))
            .order_by(DIGEST_RUNS.c.finished_at.desc()).limit(1)).mappings().first()
        return dict(row) if row else None


def digest_items_for_run(run_id: str) -> list[dict]:
    # ⚠️ `source_id` СЫРЬЯ ОБЯЗАТЕЛЕН. Без него `_render_from_db_rows` собирает
    # NewsItem с пустым source_id, блок регуляторов не находит свежих пунктов и
    # печатает архивный fallback: повторное открытие того же выпуска показывало
    # сырой заголовок ленты вместо фразы модели и теряло пункты (fallback
    # limit=2 против cap=3).
    with engine().begin() as conn:
        rows = conn.execute(
            select(
                *DIGEST_ITEMS.c,
                RAW_ITEMS.c.title, RAW_ITEMS.c.snippet, RAW_ITEMS.c.url,
                RAW_ITEMS.c.published_at, RAW_ITEMS.c.matched_json,
                RAW_ITEMS.c.source_id,
                SOURCES.c.display_name.label("source_name"),
            ).select_from(
                DIGEST_ITEMS
                .join(RAW_ITEMS, RAW_ITEMS.c.raw_hash == DIGEST_ITEMS.c.raw_hash)
                .join(SOURCES, SOURCES.c.source_id == RAW_ITEMS.c.source_id))
            .where(DIGEST_ITEMS.c.run_id == run_id)
            .order_by(DIGEST_ITEMS.c.final_score.desc())).mappings().all()
        return [dict(r) for r in rows]


def search_digest_items(query: str, *, days: int = 7, limit: int = 5) -> list[dict]:
    """Поиск по УЖЕ СОБРАННЫМ новостям — то, чего агенту не хватало (17.08.2026).

    ЗАЧЕМ. Владелец спросил про новость из только что собранного выпуска, а
    агент ответил «не нашёл в дайджесте» и полез в интернет: прочитать
    собранное ему было НЕЧЕМ — ни один инструмент не отдавал ни `raw_items`,
    ни `digest_items`. Данные при этом лежали на месте: заголовок, изложение
    из ленты, ссылка, и сверх того фраза, которую написала сама модель при
    классификации (`one_sentence`, `why_important`).

    Ищем по всем четырём текстовым полям сразу: пользователь спрашивает
    словами из заголовка («Claude», «акции Uber»), а полезный ответ чаще
    лежит в изложении.

    Окно в днях, а не «последний прогон»: разговор о выпуске продолжается и
    назавтра, а прогонов за день бывает несколько.
    """
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    cutoff = time.time() - days * 86400
    from sqlalchemy import or_ as sa_or  # noqa: PLC0415

    with engine().begin() as conn:
        rows = conn.execute(
            select(
                RAW_ITEMS.c.title, RAW_ITEMS.c.snippet, RAW_ITEMS.c.url,
                RAW_ITEMS.c.published_at,
                SOURCES.c.display_name.label("source_name"),
                DIGEST_ITEMS.c.one_sentence, DIGEST_ITEMS.c.why_important,
                DIGEST_ITEMS.c.category, DIGEST_ITEMS.c.importance,
                DIGEST_ITEMS.c.watchlist_match, DIGEST_ITEMS.c.final_score,
            ).select_from(
                DIGEST_ITEMS
                .join(RAW_ITEMS, RAW_ITEMS.c.raw_hash == DIGEST_ITEMS.c.raw_hash)
                .join(SOURCES, SOURCES.c.source_id == RAW_ITEMS.c.source_id)
                .join(DIGEST_RUNS, DIGEST_RUNS.c.run_id == DIGEST_ITEMS.c.run_id))
            .where(and_(
                DIGEST_RUNS.c.started_at > cutoff,
                sa_or(RAW_ITEMS.c.title.like(like),
                      RAW_ITEMS.c.snippet.like(like),
                      DIGEST_ITEMS.c.one_sentence.like(like),
                      DIGEST_ITEMS.c.why_important.like(like))))
            .order_by(DIGEST_ITEMS.c.final_score.desc())
            .limit(int(limit))).mappings().all()
        return [dict(r) for r in rows]


def recent_long_list_hashes(days: int = 7, user_id: int | None = None) -> set[str]:
    """raw_hash всего, что показывалось в long list ЭТОМУ пользователю за N дней.

    Межпрогонный дедуп (#94, фидбек потребителей): окно сбора 48ч при ежедневном
    прогоне легально протаскивает вчерашние истории в сегодняшний дайджест
    (на проде 3/16 позиций long list были точными повторами вчерашнего).
    Повторы уходят в «Прочие», а long list заполняется свежим.

    🔴 ОТБОР ПО ВЛАДЕЛЬЦУ (18.08.2026). Прежняя редакция брала ВСЕ прогоны
    подряд — привязки к пользователю в таблице не было вовсе. На проде это
    давало 135 хешей от 18 чужих прогонов за неделю: история, которую читал
    один пользователь, молча уходила из топа у второго, никогда её не видевшего.
    Он получал в long list не самое важное, а то, что осталось после чужого
    чтения. Прогоны планировщика (`user_id IS NULL`) исключены осознанно: их
    выпуск не читает никто, он пишется в лог и греет кэш сырья.

    `user_id=None` (планировщик) — дедупа нет: подавлять ему нечего.
    """
    if user_id is None:
        return set()
    with engine().begin() as conn:
        rows = conn.execute(
            select(DIGEST_ITEMS.c.raw_hash).distinct().select_from(
                DIGEST_ITEMS.join(DIGEST_RUNS,
                                  DIGEST_RUNS.c.run_id == DIGEST_ITEMS.c.run_id))
            .where(and_(DIGEST_ITEMS.c.in_long_list == 1,
                        DIGEST_RUNS.c.started_at >= time.time() - days * 86400,
                        DIGEST_RUNS.c.user_id == user_id))).all()
    return {r[0] for r in rows}
