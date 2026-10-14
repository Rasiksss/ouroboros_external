"""Дедуп считает, что прочитал ЭТОТ читатель, а не кто угодно (18.08.2026).

ВОПРОС ВЛАДЕЛЬЦА: «межпрогонный дедуп — от пользователя к пользователю работает
или только внутри одного?» Оказалось — глобально, и это дефект.

`recent_long_list_hashes` отбирала показанное в long list по ВСЕМ прогонам
подряд. Привязки прогона к пользователю в таблице не было вовсе: у
`digest_runs` колонки `user_id` не существовало, а `profile` у всех один и тот
же (`ir_market_news`).

ЗАМЕР НА ПРОДЕ: 18 прогонов за неделю, 135 хешей в общем множестве подавления,
вклад отдельных прогонов 10–19 позиций. На проде три владельца сценариев IR
Daily (1, 5, 6). То есть история, которую прочитал один, молча уходила из топа
у второго, никогда её не видевшего: он получал в long list не самое важное, а
то, что осталось после чужого чтения.

Отдельно исключены прогоны планировщика (`user_id IS NULL`): его выпуск не
читает никто — он пишется в лог и греет кэш сырья, — а в общее множество
подавления добавлял по 15–19 позиций ежедневно.
"""
from __future__ import annotations

import inspect
import re

import pytest

from finengine.skills.news import db, pipeline, render
from finengine.skills.news.models import ClassifiedItem, NewsItem

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")



@pytest.fixture(autouse=True)
def _isolated_db(бд_хранилищ, monkeypatch, tmp_path):
    from finengine import config as _config
    monkeypatch.setattr(_config.settings, "data_dir", tmp_path)
    db.init_schema()
    # raw_items ссылается на sources внешним ключом.
    db.upsert_source(source_id="rbc", display_name="РБК", source_type="tier1",
                     region="RU", url="https://rbc.ru/", weight=0.8)


def _item(raw_hash: str) -> NewsItem:
    from datetime import datetime, timezone
    return NewsItem(
        source="РБК", source_id="rbc", source_type="tier1", region="RU",
        source_weight=0.8, title="t", snippet="s",
        url=f"https://rbc.ru/{raw_hash}",
        published_at=datetime.now(timezone.utc), raw_hash=raw_hash,
    )


def _classified(raw_hash: str) -> ClassifiedItem:
    # digest_items ссылается на raw_items внешним ключом — сырьё должно
    # существовать до записи в выпуск.
    db.upsert_raw_item(_item(raw_hash))
    return ClassifiedItem(
        item=_item(raw_hash), is_relevant=True, category="corporate",
        region="RU", importance=0.9,
        one_sentence_ru="Событие", why_important="важно",
    )


def test_one_readers_history_does_not_suppress_anothers():
    """Отрицательный контроль: убрал `AND r.user_id = ?` — краснеет.

    Именно так вело себя прежнее поведение: 135 чужих хешей в подавлении.
    """
    db.start_run("run-alice", "ir_market_news", user_id=1)
    from finengine.skills.news.models import DigestItem

    db.insert_digest_items("run-alice", [
        DigestItem(classified=_classified("h-alice"), final_score=0.9,
                   in_long_list=True)])

    assert db.recent_long_list_hashes(days=7, user_id=1) == {"h-alice"}
    assert db.recent_long_list_hashes(days=7, user_id=2) == set(), (
        "чужая история подавляет мою — я получу в long list остатки чужого чтения")


def test_scheduler_run_poisons_nobody():
    """Прогон планировщика (`user_id=None`) не читает никто: он пишется в лог и
    греет кэш. В подавление он попадать не должен."""
    from finengine.skills.news.models import DigestItem

    db.start_run("run-cron", "ir_market_news", user_id=None)
    db.insert_digest_items("run-cron", [
        DigestItem(classified=_classified("h-cron"), final_score=0.9,
                   in_long_list=True)])

    assert db.recent_long_list_hashes(days=7, user_id=1) == set()
    assert db.recent_long_list_hashes(days=7, user_id=None) == set(), (
        "у планировщика нет читателя — подавлять ему нечего")


def test_pipeline_passes_the_owner_to_both_places():
    """Колонка бесполезна, если конвейер её не заполняет и не спрашивает —
    ровно тот разрыв «умеет, но не доехало», на котором мы горим весь день."""
    src = inspect.getsource(pipeline.run_daily_news_pipeline)
    assert "start_run(run_id, profile, user_id=user_id)" in src, (
        "владелец не записывается в прогон")
    assert "recent_long_list_hashes(days=7, user_id=user_id)" in src, (
        "дедуп спрашивается без владельца — снова станет общим")


def test_migration_adds_the_column_on_a_live_db(бд_хранилищ):
    """У базы прода 87+ прогонов и 143 МБ рядом: пересоздавать таблицу нельзя,
    колонка добавляется ALTER-ом. Старые строки получают NULL — то есть
    попадают в «прогоны без читателя» и никого не подавляют.

    🔴 ПЕРЕПИСАН 25.08 ВМЕСТЕ С ПРЕДМЕТОМ. Тест звал `db.connect()` и
    `PRAGMA table_info` — обоих больше нет: модуль переведён на SQLAlchemy, а
    `PRAGMA` есть только у SQLite и на Postgres уронил бы проверку. Свойство
    прежнее, способ спросить — инспектор.

    Заодно тест перестал зависеть от того, какая база подвернулась процессу:
    старая редакция смотрела на ambient `db_path()` и была зелёной просто
    потому, что колонку кто-то добавил раньше.
    """
    from sqlalchemy import inspect as sa_inspect, text

    # ⚠️ 26.08.2026: «живая база до ALTER-а» воспроизводится ТАМ ЖЕ, где живёт
    # настоящая, — в Postgres. Раньше здесь заводился файл SQLite; файлового
    # пути у модуля больше нет, а свойство проверяется то же: `init_schema` на
    # таблице без новых колонок дописывает их, а не пересоздаёт её.
    дв = db.engine()
    with дв.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS digest_runs CASCADE"))
        c.execute(text("""CREATE TABLE digest_runs (
            run_id TEXT PRIMARY KEY, started_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION, profile TEXT NOT NULL,
            items_raw INTEGER, items_kept INTEGER, llm_in INTEGER,
            llm_out INTEGER, cost_usd DOUBLE PRECISION, error TEXT)"""))

    db.init_schema()

    колонки = {c["name"] for c in sa_inspect(db.engine()).get_columns("digest_runs")}
    assert "user_id" in колонки, "колонка владельца не добавлена на живой базе"
    assert "diag" in колонки, "колонка разбора не добавлена"


def test_coverage_target_is_twenty():
    """Решение владельца 18.08: добор пустых клеток сетки — 20 сверх
    пятнадцати, было 10. Сетка 5 регионов × 3 категории = 15 клеток."""
    src = inspect.getsource(render.select_long_list)
    m = re.search(r"coverage_target\s*=\s*(\d+)", src)
    assert m and int(m.group(1)) == 20, "потолок добора не 20"
    regions = re.search(r'for r in \(([^)]+)\)', src)
    assert regions and regions.group(1).count(",") == 4, (
        "в сетке не пять регионов — комментарий про 5×3 разойдётся с кодом")
