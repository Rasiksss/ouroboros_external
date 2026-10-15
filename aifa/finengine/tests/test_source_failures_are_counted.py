"""Отказы источников доезжают до счётчика (17.08.2026).

ЗАЧЕМ. `source_stats.failed` показывал НОЛЬ при живых отказах. Замер на проде
17.08: 19 отказов Investing (403), `bea_news` (сеть недоступна), три EDGAR
(таймаут), `pr_newswire` (404), `techcrunch` (битый XML) — и `failed: {}`.
Причина: `_fetch_one` записывал отказ только когда коннектор БРОСАЛ, а по
контракту (`connectors/base.py`) он не бросает никогда — гасит ошибку, пишет
предупреждение и отдаёт пустой список.

Владелец диагностирует этой строкой «почему новостей нет». Пока она врала,
мёртвый источник и источник без новостей выглядели одинаково.
"""
from __future__ import annotations

import logging

import pytest

from finengine.skills.news.collect import _ConnectorFailureLog

CONN_LOG = "finengine.skills.news.connectors"


@pytest.fixture()
def sink():
    s = _ConnectorFailureLog({"cbr_press": "ЦБ РФ", "techcrunch": "TechCrunch"})
    log = logging.getLogger(CONN_LOG)
    log.addHandler(s)
    yield s
    log.removeHandler(s)


def test_connector_warning_becomes_a_counted_failure(sink):
    """Отрицательный контроль: снял перехват — счётчик снова пуст."""
    logging.getLogger(CONN_LOG + ".rss").warning("RSS %s: HTTP %d", "techcrunch", 404)
    assert sink.by_source == {"TechCrunch": "RSS techcrunch: HTTP 404"}


def test_every_failure_path_is_caught_not_just_exceptions(sink):
    """Формы отказа у коннекторов разные — ловиться должны все."""
    lg = logging.getLogger(CONN_LOG + ".rss")
    lg.warning("RSS fetch failed for %s: %s", "cbr_press", "Network is unreachable")
    lg.warning("RSS %s: HTTP %d", "techcrunch", 403)
    assert set(sink.by_source) == {"ЦБ РФ", "TechCrunch"}


def test_first_failure_wins_so_one_source_is_not_double_counted(sink):
    lg = logging.getLogger(CONN_LOG + ".rss")
    lg.warning("RSS %s: HTTP %d", "techcrunch", 500)
    lg.warning("RSS parse failed for %s: %s", "techcrunch", "bad xml")
    assert sink.by_source["TechCrunch"].endswith("HTTP 500")


def test_unattributable_warning_is_dropped(sink):
    """Запись без имени источника молчит: приписать отказ не тому источнику
    хуже, чем не показать его вовсе."""
    logging.getLogger(CONN_LOG).warning("feedparser not installed")
    assert sink.by_source == {}


def test_info_level_is_not_a_failure(sink):
    """Успешный забор пишется в INFO — он не отказ."""
    logging.getLogger(CONN_LOG + ".rss").info(
        "RSS %s: fetched %d items", "cbr_press", 12)
    assert sink.by_source == {}


def test_message_is_trimmed():
    s = _ConnectorFailureLog({"x": "X"})
    log = logging.getLogger(CONN_LOG)
    log.addHandler(s)
    try:
        log.warning("x " + "д" * 500)
    finally:
        log.removeHandler(s)
    assert len(s.by_source["X"]) <= 120
