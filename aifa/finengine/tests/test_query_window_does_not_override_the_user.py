"""Окно запроса к источнику не должно быть уже периода сбора (17.08.2026).

НАЙДЕНО ПРОВЕРКОЙ ВСЕХ 112 RSS-ИСТОЧНИКОВ НА ПРОДЕ. Одиннадцать отдавали ноль
записей, и десять из них — поиски Google News по компаниям. Причина оказалась
не в поломке: в запрос было зашито `when:24h`, то есть окно СУТКИ.

Хуже, чем пустые ленты: это молча обрезало пользовательскую ручку «период
сбора». Ставишь 48 часов или неделю — а по компаниям всё равно приезжают только
последние сутки, и понять это по интерфейсу невозможно.

ЗАМЕР, тот же час, те же источники:
    gn_icbc        24h → 0 записей   |  7d → 3
    gn_raiffeisen  24h → 0 записей   |  7d → 7
    gn_weride      24h → 0 записей   |  7d → 4

Лишнего не приедет: дату отсекает НАШ фильтр по `since_hours`
(`connectors/rss.py`). Запрос лишь не должен решать за пользователя.
"""
from __future__ import annotations

import re

import pytest

from finengine.skills.news import config as _cfg


def _google_news_sources():
    return [s for s in _cfg.load().sources if "news.google.com" in (s.url or "")]


def test_there_are_google_news_sources_at_all():
    """Иначе проверки ниже были бы зелёными на пустом месте."""
    assert _google_news_sources(), "источников Google News не найдено"


@pytest.mark.parametrize("hours_in_window", [("when:24h", 24), ("when:12h", 12), ("when:1h", 1)])
def test_narrow_windows_are_gone(hours_in_window):
    """Отрицательный контроль: вернул `when:24h` — краснеет."""
    needle = hours_in_window[0]
    for s in _google_news_sources():
        assert needle not in (s.url or ""), (
            f"{s.id}: окно запроса {needle} уже пользовательского периода сбора")


def test_generated_company_sources_use_the_week_window():
    """Компанейские ленты рождаются из companies.yaml — правка должна была
    попасть в генератор, а не в отдельные записи каталога.

    🔵 21.08.2026: сторож читал `config.py` и искал `when:7d` в строке сборки
    запроса. Мутация «`sources = sources  # + _company_sources()`» оставляла его
    зелёным, а из каталога исчезали 85 лент из 88 — три статические остальные
    проверки этого файла удовлетворяли. Теперь генератор ЗОВЁТСЯ, и его выдача
    сверяется с тем, что реально попало в каталог.
    """
    сгенерированные = [s for s in _cfg._company_sources()
                       if "news.google.com" in (s.url or "")]
    assert len(сгенерированные) > 10, (
        f"генератор дал {len(сгенерированные)} компанейских лент — "
        "companies.yaml не читается или ленты перестали рождаться")
    узкие = [s.id for s in сгенерированные if "when:7d" not in (s.url or "")]
    assert not узкие, f"окно в запросе сужено у {узкие}"

    в_каталоге = {s.id for s in _cfg.load().sources}
    потерялись = sorted({s.id for s in сгенерированные} - в_каталоге)
    assert not потерялись, (
        f"генератор ленты сделал, а в каталог они не попали: {потерялись[:5]} "
        f"(всего {len(потерялись)}) — правка до сборщика не доехала")


def test_window_covers_the_widest_user_setting():
    """Смысл правила: окно запроса не уже того, что пользователь вправе
    попросить. 7 дней против 14 — компромисс: Google News на больших окнах
    начинает подмешивать давнее, а наш фильтр всё равно режет по дате."""
    for s in _google_news_sources():
        m = re.search(r"when(?:%3A|:)(\d+)([hd])", s.url or "")
        if not m:
            continue
        hours = int(m.group(1)) * (24 if m.group(2) == "d" else 1)
        assert hours >= 48, f"{s.id}: окно {hours} ч уже периода сбора по умолчанию (48 ч)"


def test_our_own_date_filter_is_still_the_one_that_cuts(monkeypatch):
    """Ключевое: расширив окно запроса, мы НЕ впустили старое в выпуск —
    отсечка по дате осталась на нашей стороне.

    🔵 21.08.2026: сторож искал в `connectors/rss.py` слова `cutoff` и
    `since_hours`. Мутация «`if False and published < cutoff:`» оставляла его
    зелёным при мёртвой отсечке — то есть ровно тогда, когда семидневное окно
    запроса начинает тащить старьё в выпуск. Теперь лента отдаётся коннектору.
    """
    import email.utils
    from datetime import datetime, timedelta, timezone

    import httpx

    from finengine.skills.news.connectors.rss import RSSConnector

    def _rfc822(hours_ago: int) -> str:
        return email.utils.format_datetime(
            datetime.now(timezone.utc) - timedelta(hours=hours_ago))

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<item><title>Свежая</title><link>https://example.com/1</link>"
        f"<pubDate>{_rfc822(2)}</pubDate></item>"
        f"<item><title>Позавчерашняя</title><link>https://example.com/2</link>"
        f"<pubDate>{_rfc822(100)}</pubDate></item>"
        "</channel></rss>"
    ).encode("utf-8")

    class _Resp:
        status_code = 200
        content = feed

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    source = next(iter(_google_news_sources()))
    items = RSSConnector(source).fetch(since_hours=48)
    titles = [i.title for i in items]
    assert titles == ["Свежая"], (
        f"отсечка по дате не сработала: {titles} — широкое окно запроса "
        "потащило старьё в выпуск")
