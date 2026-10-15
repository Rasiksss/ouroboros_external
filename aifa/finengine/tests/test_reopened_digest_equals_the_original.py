"""Повторно открытый выпуск равен первичному (18.08.2026).

НАЙДЕНО ДВУМЯ СОСТЯЗАТЕЛЬНЫМИ ЛИНЗАМИ НЕЗАВИСИМО, обе воспроизвели прогоном.

Дайджест отдаётся дважды разными путями: первый раз — свежесобранный
(`render_digest` по `ClassifiedItem`), потом — из БД (`api/main.py` →
`pipeline.get_cached_digest` → `_render_from_db_rows` по строкам
`db.digest_items_for_run`). Пользователь, открывший вчерашний выпуск, видит
ВТОРОЙ путь.

Блоки регуляторов расходились дословно:

    первичный   - 🏦 ЦБ РФ снизил ключевую ставку до 16% годовых
    повторный   - 🗄️ (сегодня) Ключевая ставка снижена до 16%

Причина: `_render_from_db_rows` собирает NewsItem из `r.get("source_id", "")`,
а `digest_items_for_run` эту колонку не выбирал. Значит на повторной отдаче у
ВСЕХ пунктов `source_id == ""`, `_render_regulators_box` не находит ни одного
свежего и уходит в архивный fallback: фраза модели подменяется сырым заголовком
ленты, а пунктов остаётся меньше (fallback `limit=2` против `cap=3` у свежих) —
и число в шапке тоже расходится.

🔴 ПЕРЕПИСАН 21.08.2026 С РЕГУЛЯРКИ НА ПРОГОН. Прежняя редакция брала
`inspect.getsource` обеих функций, вытаскивала регуляркой имена колонок из
`SELECT` и имена ключей из `.get(...)` и сравнивала два множества строк. Она
охраняла ФОРМУ ЗАПИСИ, а не результат: колонка, приехавшая через `d.*`, через
`AS` с переносом строки или прочитанная в переменную, для регулярки не
существует, и любой такой разъезд проехал бы зелёным. Теперь выпуск
собирается, кладётся в настоящую sqlite, читается обратно тем же запросом и
рендерится вторым путём — сверяется САМ MARKDOWN, побайтово.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from finengine.skills.news import config as _cfg
from finengine.skills.news import db, pipeline
from finengine.skills.news import macro_indicators, render
from finengine.skills.news.models import ClassifiedItem, DigestItem, NewsItem

# Файловых запасных путей у новостей, сценариев и реестра документов больше нет
# (снесены 26.08.2026) — файлу нужна настоящая база.
pytestmark = pytest.mark.usefixtures("бд_хранилищ")


# Часы фиксируем: шапка выпуска печатает «HH:MM MSK», и два рендера, разнесённые
# границей минуты, разошлись бы на ровном месте.
FROZEN = datetime(2026, 8, 18, 12, 30, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return FROZEN.astimezone(tz) if tz else FROZEN.replace(tzinfo=None)


@pytest.fixture
def digest(tmp_path, monkeypatch):
    """Один выпуск, прожитый целиком: собран → записан → прочитан обратно."""
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "news.db")
    monkeypatch.setattr(pipeline, "datetime", _FrozenDatetime)
    # Кэш макро на машине разработчика может быть непустым и разным между
    # вызовами — оба пути обязаны брать одно и то же.
    monkeypatch.setattr(macro_indicators, "_read_cache", lambda: None)
    db.init_schema()

    item = NewsItem(
        source="ЦБ РФ", source_id="cbr_press", source_type="official",
        region="RU", source_weight=0.9,
        title="ЦБ РФ снизил ключевую ставку до 16% годовых",
        snippet="Совет директоров принял решение.",
        url="https://cbr.ru/press/pr/?file=1", published_at=FROZEN,
        raw_hash="hash-1", matched_entities=["ЦБ РФ"], matched_topics=["ставка"])
    classified = ClassifiedItem(
        item=item, is_relevant=True, category="regulator", region="RU",
        importance=0.9, one_sentence_ru="Ключевая ставка снижена до 16%.",
        why_important="определяет стоимость денег")

    db.upsert_source("cbr_press", "ЦБ РФ", "official", "RU", "https://cbr.ru", 0.9, True)
    db.upsert_raw_item(item)
    db.start_run("run-1", "ir_market_news")
    db.insert_digest_items(
        "run-1", [DigestItem(classified=classified, final_score=0.9, in_long_list=True)])

    rows = db.digest_items_for_run("run-1")
    first, _ = render.render_digest(
        [classified], cfg=_cfg.load(), now=FROZEN.astimezone(pipeline._MSK),
        macro_payload={})
    return classified, rows, first, pipeline._render_from_db_rows(rows)


def test_the_run_actually_round_tripped(digest):
    """Опора: пустая выборка сделала бы сверку ниже зелёной ни о чём."""
    _classified, rows, first, _again = digest
    assert len(rows) == 1, f"из БД вернулось {len(rows)} строк вместо одной"
    assert "16%" in first, "первичный выпуск не содержит самой новости"


def test_reopening_gives_the_same_digest(digest):
    """ГЛАВНОЕ: повторная отдача побайтово равна первичной.

    Отрицательный контроль (проведён 21.08.2026): убрать `r.source_id` из
    `SELECT` в `db.digest_items_for_run` — тест краснеет и показывает диффом
    подмену фразы модели сырым заголовком ленты. Это и был живой дефект:
    колонка читалась через `.get(..., "")`, поэтому её отсутствие не падало.
    """
    _classified, _rows, first, again = digest
    if first != again:
        import difflib
        diff = "\n".join(difflib.unified_diff(
            first.splitlines(), again.splitlines(),
            fromfile="первичный", tofile="повторный", lineterm=""))
        pytest.fail(
            "выпуск из БД разошёлся с первичным — пользователь, открывший "
            f"вчерашний выпуск, увидит другой текст:\n{diff}")


def test_the_regulator_line_survives_the_round_trip(digest):
    """Отдельно про блок, который расходился: фраза МОДЕЛИ, а не заголовок ленты.

    Общая сверка выше покраснела бы и от расхождения в шапке; здесь названо
    именно то, что теряется при пустом `source_id` — пункт уходит в архивный
    fallback, где печатается сырой `title` вместо `one_sentence_ru`.
    """
    classified, _rows, _first, again = digest
    # Точка на конце срезается самим рендером (`rstrip(".")`).
    assert classified.one_sentence_ru.rstrip(".") in again, (
        "в повторной отдаче нет фразы модели — блок регуляторов уехал в "
        "архивный fallback, значит источник пункта потерялся по дороге")
    assert "🗄️" not in again, (
        "пункт напечатан как архивный, хотя он свежий: `source_id` не доехал "
        "до `_render_regulators_box`")
