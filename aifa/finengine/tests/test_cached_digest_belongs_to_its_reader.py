"""Открыв «Дайджест», читатель видит СВОЙ выпуск (21.08.2026).

НАЙДЕНО ПРИ РАЗБОРЕ ВОПРОСА «многопользовательскую настройку проверил?».
Межпрогонный дедуп мы к владельцу привязали, а ЧТЕНИЕ ГОТОВОГО ВЫПУСКА — нет.

`latest_digest_run` брала последний удачный прогон КОГО УГОДНО. Открывая
дайджест без запуска, пользователь получал чужой: собранный по чужому окну
сбора, чужим группам источников и чужому фокусу. На проде три владельца плюс
ежедневный прогон планировщика (`user_id IS NULL`), выпуск которого не читает
никто, — то есть свой выпуск пользователь видел только сразу после
собственного запуска, а в остальное время чужой.

Разрыв был виден и внутри одного файла: инструмент `daily_news_digest`
передавал `user_id` в СВЕЖИЙ прогон и не передавал в чтение кэша — две ветки
одной функции расходились в том, чей это выпуск.

Сторож работает с настоящей sqlite и настоящими прогонами, а не ищет строки:
текстовая проверка не поймала бы ни `user_id`, потерянный на одном из трёх
вызовов, ни `IS ?`, подменённый на `= ?` (последнее тихо ломает выборку
планировщика — в SQL `NULL = NULL` ложно).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from finengine.skills.news import db, pipeline
from finengine.skills.news.models import ClassifiedItem, DigestItem, NewsItem

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")


FROZEN = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated(бд_хранилищ, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "news.db")
    db.init_schema()
    db.upsert_source("rbc", "РБК", "tier1", "RU", "https://rbc.ru/", 0.8, True)


def _run_for(owner, run_id: str, marker: str) -> None:
    """Записывает завершённый прогон владельца с одной узнаваемой новостью."""
    item = NewsItem(
        source="РБК", source_id="rbc", source_type="tier1", region="RU",
        source_weight=0.8, title=marker, snippet="",
        url=f"https://rbc.ru/{run_id}", published_at=FROZEN, raw_hash=run_id,
    )
    db.upsert_raw_item(item)
    db.start_run(run_id, "ir_market_news", user_id=owner)
    db.insert_digest_items(run_id, [DigestItem(
        classified=ClassifiedItem(
            item=item, is_relevant=True, category="corporate", region="RU",
            importance=0.9, one_sentence_ru=marker, why_important="важно"),
        final_score=0.9, in_long_list=True)])
    db.finish_run(run_id, items_raw=1, items_kept=1, llm_in=1, llm_out=1,
                  cost_usd=0.0)


def test_a_reader_gets_their_own_digest_not_the_freshest_one():
    """Отрицательный контроль: убрал `AND user_id IS ?` — краснеет.

    Прогон владельца 2 записан ПОЗЖЕ, то есть «последний удачный» — именно он.
    """
    _run_for(1, "run-alice", "Новость Алисы")
    _run_for(2, "run-bob", "Новость Боба")

    alice = pipeline.get_cached_digest(user_id=1)
    assert alice is not None, "свой выпуск не найден вовсе"
    assert "Новость Алисы" in alice["markdown"], (
        f"читателю отдан ЧУЖОЙ выпуск: {alice['markdown'][:200]}")
    assert "Новость Боба" not in alice["markdown"]


def test_the_scheduler_run_is_not_served_to_anyone():
    """Прогон планировщика (`user_id=None`) греет кэш сырья, но выпуск его
    предъявлять некому: он собран по умолчаниям, а не по чьим-то настройкам."""
    _run_for(None, "run-cron", "Новость планировщика")

    assert pipeline.get_cached_digest(user_id=1) is None, (
        "пользователю отдан выпуск планировщика")


def test_null_owner_is_matched_by_is_not_equals():
    """🔴 Ловушка SQL: `user_id = NULL` ложно ВСЕГДА, поэтому выборка
    планировщика молча опустела бы, а тест выше остался зелёным. Проверяем,
    что свой выпуск планировщик всё же находит."""
    _run_for(None, "run-cron", "Новость планировщика")

    got = pipeline.get_cached_digest(user_id=None)
    assert got is not None and "Новость планировщика" in got["markdown"], (
        "выборка по NULL-владельцу сломана — вероятно `= ?` вместо `IS ?`")


def test_reading_and_running_agree_on_the_owner():
    """Две ветки одной функции обязаны сходиться: инструмент передавал
    `user_id` в свежий прогон и терял при чтении кэша."""
    import inspect

    from finengine.llm import tools

    src = inspect.getsource(tools._h_daily_news_digest)
    i = src.index("get_cached_digest(")
    assert "user_id" in src[i:i + 120], (
        "чтение кэша идёт без владельца, а свежий прогон — с ним")
