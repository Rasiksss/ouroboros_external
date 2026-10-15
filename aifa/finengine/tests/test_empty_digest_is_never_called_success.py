"""Пустой выпуск не выдаётся за удачный (17.08.2026).

ЖИВОЙ ПРОГОН НА ПРОДЕ, запущенный по просьбе владельца «проверь, что он правду
говорит и не врёт нигде». Соврал:

    Сегодня в дайджесте: 0 компаний (из 100 в покрытии) · 0 макропоказателей ·
    0 публикаций регуляторов
    …а строкой ниже — таблица макропоказателей с данными
    …в журнале прогона: «ошибка: нет»

Собрано было 942 новости, отобрано 60 кандидатов. Классификация упала
(`ModelSilent`: 29 287 знаков размышлений, ни одного знака ответа), вернула
пустой список — и прогон записался УСПЕШНЫМ.

ПОЧЕМУ ЭТО ХУЖЕ ПАДЕНИЯ. Упавший прогон видно, и его перезапустят. Пустой
выпуск с пометкой «ошибок нет» выглядит как «сегодня новостей не было» — его
никто не перепроверит.

ПРИЧИНА БЫЛА В ДВУХ МЕСТАХ СРАЗУ:
 * `classify_batch` гасит исключение и возвращает `[], {"error": ...}`;
 * конвейер ключ `error` не смотрел.

РЕВИЗИЯ 21.08.2026. Все четыре сторожа искали строки в тексте модулей. Замена
`raise RuntimeError(...)` на `log.warning(...)` — то есть ВОЗВРАТ дефекта
слово в слово — оставляла их зелёными: искомые строки при ней на месте.
Теперь конвейер и классификация ЗАПУСКАЮТСЯ.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from finengine.skills.news import classify, pipeline
from finengine.skills.news.models import NewsItem

_ОТКАЗ = "ModelSilent: 29 287 знаков размышлений, ни одного знака ответа"


def _item() -> NewsItem:
    return NewsItem(
        source="Reuters", source_id="reuters", source_type="wire", region="world",
        source_weight=1.0, title="Fed holds rates steady", snippet="…",
        url="https://example.org/1", published_at=datetime.now(timezone.utc),
        raw_hash="h1",
    )


def _run_with_classify(monkeypatch, classify_result) -> dict:
    """Прогон конвейера, у которого собрано ОДНО событие и вся разница — в
    исходе классификации."""
    async def _collected(**kw):
        return [_item()]

    async def _classify(items, **kw):
        return classify_result

    monkeypatch.setattr(pipeline._collect, "collect_all", _collected)
    monkeypatch.setattr(pipeline._classify, "classify_batch", _classify)
    monkeypatch.setattr(pipeline._filter, "annotate", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline._filter, "filter_relevant", lambda items, cfg: items)
    monkeypatch.setattr(pipeline._filter, "dedup_near_duplicates", lambda items: items)
    monkeypatch.setattr(pipeline._rank, "top_n", lambda items, n: items)
    monkeypatch.setattr(pipeline._render, "render_digest", lambda *a, **kw: ("# выпуск", []))
    monkeypatch.setattr(pipeline._render, "take_translation_usage", dict)
    for stub in ("init_schema", "start_run", "finish_run", "set_matched",
                 "insert_digest_items"):
        monkeypatch.setattr(pipeline._db, stub, lambda *a, **kw: None)
    monkeypatch.setattr(pipeline._db, "recent_long_list_hashes",
                        lambda **kw: set())
    # Наружу за макропоказателями конвейер ходит по сети — в прогоне тестов
    # это 70 секунд ретраев на стадии, к отказу классификации отношения не
    # имеющей.
    from finengine.skills.news import macro_indicators

    async def _no_macro():
        return {}

    monkeypatch.setattr(macro_indicators, "fetch_macro_dashboard", _no_macro)
    return asyncio.run(pipeline.run_daily_news_pipeline())


def test_a_refused_classification_makes_the_run_red(monkeypatch):
    """Отказ классификации — отказ прогона, и в отказе названа стадия.

    Отрицательный контроль: заменить `raise` на `log.warning` — краснеет.
    Ровно эта подмена и была дефектом: 942 собранных новости, пустой выпуск,
    «ошибка: нет» в журнале.
    """
    out = _run_with_classify(monkeypatch, ([], {"error": _ОТКАЗ}))
    assert out["error"], "пустой выпуск снова записан УСПЕШНЫМ"
    assert "классификация не удалась" in out["error"], (
        f"по отказу не видно, ЧТО отказало: {out['error']!r}")
    assert _ОТКАЗ in out["error"], (
        f"причина отказа потеряна по дороге: {out['error']!r}")
    assert "Ошибка сборки дайджеста" in out["markdown"], (
        "на экране отказ выглядит как обычный выпуск")


def test_a_classification_that_worked_is_not_called_a_failure(monkeypatch):
    """Обратная половина: без неё «всегда красный» конвейер прошёл бы проверку
    выше. Отказа нет — прогон обязан дойти до конца."""
    out = _run_with_classify(monkeypatch, ([], {"in": 10, "out": 20, "cost_usd": 0.0}))
    assert out["error"] is None, out["error"]


def _spy_on_the_model(monkeypatch) -> dict:
    """Перехват вызова модели: `classify_batch` берёт её лениво, изнутри."""
    from finengine.llm import openrouter

    seen: dict = {}

    async def _boom(**kw):
        seen.update(kw)
        raise RuntimeError(_ОТКАЗ)

    monkeypatch.setattr(openrouter, "complete_with_usage", _boom)
    return seen


def test_classify_reports_the_error_it_swallows(monkeypatch):
    """Классификация гасит исключение — значит обязана СООБЩИТЬ о нём в usage.
    Иначе проверять конвейеру будет нечего."""
    _spy_on_the_model(monkeypatch)
    classified, usage = asyncio.run(classify.classify_batch([_item()]))
    assert classified == []
    assert _ОТКАЗ in str(usage.get("error", "")), (
        f"причина отказа не попала в usage: {usage!r}")


def test_the_budget_accounts_for_a_reasoning_model(monkeypatch):
    """Бюджет 9000 был рассчитан на модель БЕЗ размышлений. У рассуждающей
    токены размышлений идут в тот же счёт: живой прогон сжёг 29 287 знаков и
    до ответа не дошёл.

    🔴 ПЕРЕПИСАН 21.08.2026 С ЛИТЕРАЛА НА ФОРМУЛУ. Здесь стояло
    `>= 24000` — число, подобранное под потолок в 60 кандидатов. Потолок стал
    настройкой (`news_top_candidates`, по умолчанию 100), и прибитый литерал
    начал охранять не то: на батче любого размера он требовал одну и ту же
    величину. Проверяем ИНВАРИАНТ — бюджет покрывает сам батч и оставляет
    запас на размышления, — а не конкретное число.
    """
    seen = _spy_on_the_model(monkeypatch)
    batch = [_item() for _ in range(60)]
    asyncio.run(classify.classify_batch(batch))
    got = seen.get("max_output", 0)
    # Верх замера по 12 живым прогонам — 332 токена ответа на кандидата.
    assert got >= 332 * len(batch), (
        f"бюджет {got} ниже замеренного верха ответа на {len(batch)} кандидатов")
    assert got - 332 * len(batch) >= 5000, (
        f"бюджет {got} не оставляет запаса на размышления — живой отказ был "
        "на 29 287 знаках рассуждений при нулевом ответе")
