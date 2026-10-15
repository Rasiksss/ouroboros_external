"""Заголовки Google News приходят без хвоста «- Издание» (17.08.2026).

ЗАМЕР НА БОЕВОМ ПРОДЕ. Google News отдаёт заголовок как «Заголовок - Издание»,
и хвост ехал в выпуск дословно. Хуже того, у части записей заголовка не было
вовсе — оставалось голое «- GlobeNewswire»: 26 из 229 у одной ленты, 146 за
48 часов по всем. Дедуп их не ловит, потому что формально они разные.

Издание при этом не теряется: оно едет в `source` и попадает в выпуск
отдельным полем. В заголовке это шум.
"""
from __future__ import annotations

import pytest

from finengine.skills.news.connectors.rss import _strip_google_news_publisher as strip


@pytest.mark.parametrize(("raw", "want"), [
    ("NBPE Announces Transaction in Own Shares - GlobeNewswire",
     "NBPE Announces Transaction in Own Shares"),
    ("DIAGNOS Receives Saudi FDA Medical Device License for CARA - GlobeNewswire",
     "DIAGNOS Receives Saudi FDA Medical Device License for CARA"),
    ("Fed holds rates steady - Reuters", "Fed holds rates steady"),
])
def test_publisher_tail_is_removed(raw, want):
    """Отрицательный контроль: снял вызов из коннектора — хвост возвращается."""
    assert strip(raw) == want


@pytest.mark.parametrize("raw", ["- GlobeNewswire", " - Reuters", "- X"])
def test_headless_entry_is_dropped(raw):
    """Пустой заголовок — отбраковка. Имя агентства вместо заголовка хуже,
    чем отсутствие новости: именно оно и попадало в выпуск."""
    assert strip(raw) == ""


def test_only_the_last_dash_is_cut():
    """У заголовков бывают свои тире — резать надо ПОСЛЕДНее, иначе потеряем
    половину смысла."""
    raw = "Q2 2026 results - revenue up 12% - GlobeNewswire"
    assert strip(raw) == "Q2 2026 results - revenue up 12%"


def test_title_without_a_tail_is_untouched():
    """Не всякий заголовок имеет хвост — трогать его нельзя."""
    assert strip("ЦБ повысил ключевую ставку") == "ЦБ повысил ключевую ставку"


def _feed_with(title: str) -> bytes:
    """Лента из одной записи со свежей датой — иначе окно `since_hours` её срежет."""
    from email.utils import format_datetime
    from datetime import datetime, timezone
    now = format_datetime(datetime.now(timezone.utc))
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        f"<item><title>{title}</title><link>https://example.org/1</link>"
        f"<pubDate>{now}</pubDate></item></channel></rss>"
    ).encode("utf-8")


def _titles_from(url: str, body: bytes, monkeypatch) -> list[str]:
    """Пропустить ленту через настоящий `RSSConnector.fetch`."""
    import httpx

    from finengine.skills.news.config import SourceConfig
    from finengine.skills.news.connectors.rss import RSSConnector

    class _Resp:
        status_code = 200
        content = body

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    source = SourceConfig(id="s", display_name="S", type="wire", region="world",
                          connector="rss", url=url, weight=1.0)
    return [item.title for item in RSSConnector(source).fetch()]


def test_only_google_news_sources_are_stripped(monkeypatch):
    """Правило применяется по URL источника, а не ко всем лентам подряд:
    у обычных RSS тире в заголовке — часть текста.

    Прогоняем ленту через `fetch`, а не ищем «news.google.com» в его тексте:
    признак, дописанный `or True`, текстовому сторожу невидим, и он зеленел,
    пока правило резало заголовки ВСЕХ лент (мутация проверена 21.08).
    """
    raw = "Северсталь - итоги - Интерфакс"
    assert _titles_from("https://news.google.com/rss/search?q=x",
                        _feed_with(raw), monkeypatch) == ["Северсталь - итоги"]
    assert _titles_from("https://feeds.reuters.com/business",
                        _feed_with(raw), monkeypatch) == [raw]
