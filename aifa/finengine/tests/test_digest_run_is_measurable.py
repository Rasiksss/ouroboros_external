"""Прогон дайджеста оставляет след для разбора (17.08.2026).

ЗАЧЕМ. Весь разбор 17.08 пришлось вести по живым процессам и логам контейнера,
потому что в журнале прогона не оставалось ничего, кроме числа новостей и
стоимости. А вопросы были ровно те, на которые отвечают эти поля:
  «почему прогон идёт шесть минут» — где именно время, в сборе или в модели;
  «почему новостей мало»           — сколько источников дало, молчало, упало;
  «сколько стоит на другой модели» — какой моделью считали этот прогон.

Поля кладутся в `digest_runs.diag` одной JSON-строкой: набор будет меняться по
мере разбора, и заводить под каждое своё поле — множить миграции.
"""
from __future__ import annotations

import json

from finengine.skills.news.pipeline import _build_diag

STATS = {
    "sources_total": 180,
    "per_source": {"ЦБ РФ": 12, "Ведомости": 50, "TechCrunch": 0,
                   "Reuters": 0, "МолчунA": 0},
    "failed": {"TechCrunch": "RSS techcrunch: HTTP 404"},
}
MARKS = {"collect_s": 331.0, "classify_s": 18.4,
         "model": "deepseek/deepseek-v4-flash", "candidates": 60}


def test_source_health_is_recorded_as_three_numbers():
    """Отрицательный контроль: убрал source_stats — счётчики схлопываются в
    нули, и мёртвую ленту снова не видно."""
    d = _build_diag(353.1, STATS, MARKS)
    assert d["sources_total"] == 180
    assert d["sources_with_news"] == 2          # ЦБ РФ + Ведомости
    assert d["sources_failed"] == 1             # TechCrunch
    assert d["sources_silent"] == 2             # Reuters + МолчунA, без отказа


def test_failed_sources_are_named_not_just_counted():
    """Счётчик говорит «одна упала», а чинить надо конкретную."""
    d = _build_diag(353.1, STATS, MARKS)
    assert d["failed_names"] == ["TechCrunch"]


def test_silent_and_failed_are_not_confused():
    """Молчащий и упавший — разные диагнозы: первый может быть нормой,
    второй всегда дефект. Слить их значило бы потерять весь смысл."""
    d = _build_diag(1.0, STATS, MARKS)
    assert d["sources_silent"] + d["sources_failed"] + d["sources_with_news"] == 5


def test_stage_timings_and_model_travel_through():
    """На прогоне 17.08 из 353 секунд почти все ушли в сбор — доказать это
    можно было только секундомером рядом."""
    d = _build_diag(353.1, STATS, MARKS)
    assert d["collect_s"] == 331.0
    assert d["classify_s"] == 18.4
    assert d["model"] == "deepseek/deepseek-v4-flash"
    assert d["duration_s"] == 353.1


def test_diag_survives_empty_input():
    """Прогон мог упасть до сбора — запись всё равно обязана появиться,
    иначе разбирать отказ будет нечем."""
    d = _build_diag(0.4, None, None)
    assert d["duration_s"] == 0.4
    assert d["sources_total"] == 0
    assert "failed_names" not in d


def test_diag_is_json_serialisable():
    """Кладётся в текстовую колонку — несериализуемое поле уронило бы запись
    прогона целиком."""
    json.dumps(_build_diag(1.0, STATS, MARKS), ensure_ascii=False)
