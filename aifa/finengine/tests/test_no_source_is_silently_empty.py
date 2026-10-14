"""Ни один источник не должен быть «живым, но пустым» (17.08.2026).

КЛАСС ОТКАЗА, КОТОРЫЙ МЫ ПРОЗЕВАЛИ. Лента отвечает HTTP 200, фид разбирается
без ошибок — и содержит НОЛЬ записей. Ни счётчик отказов, ни проверка кода
ответа такого не видят: формально всё хорошо. `reuters_business` жил так с
момента заведения и не отдал НИ ОДНОЙ новости за всё время.

Причина оказалась в операторе поиска: `allinurl:reuters.com` в лентах Google
News не работает и молча даёт пустую выдачу. Рядом, у globenewswire и
businesswire, всё это время стоял рабочий `site:` — надо было просто повторить
образец. Замер 17.08: `allinurl:` → 0 записей, `site:` → 100.

Сторож проверяет ФОРМУ адресов, а не ходит в сеть: сетевая проверка была бы
нестабильной и медленной, а ошибка была именно в форме запроса.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from finengine.skills.news import config as _cfg


def _google_news_sources():
    return [s for s in _cfg.load().sources if "news.google.com" in (s.url or "")]


def _query_of(url: str) -> str:
    return unquote(parse_qs(urlsplit(url).query).get("q", [""])[0])


def test_no_source_uses_the_operator_that_returns_nothing():
    """Отрицательный контроль: вернул `allinurl:` — краснеет.

    Оператор не «плохой стиль», а неработающий: Google News по нему отдаёт
    пустую выдачу, и источник выглядит исправным.
    """
    bad = [s.id for s in _google_news_sources() if "allinurl:" in _query_of(s.url)]
    assert not bad, f"источники с неработающим оператором allinurl: {bad}"


def test_domain_filters_use_the_form_that_works():
    """Ленты, ограниченные доменом, обязаны делать это через `site:` —
    единственную форму, которая у нас проверена на выдаче."""
    for s in _google_news_sources():
        q = _query_of(s.url)
        if any(d in q for d in ("reuters.com", "prnewswire.com",
                                "globenewswire.com", "businesswire.com")):
            assert "site:" in q, f"{s.id}: домен ограничен не через site: — {q}"


@pytest.mark.parametrize("dead", [
    "prnewswire.com/rss/news-releases-list.rss",   # 404 с 17.08.2026
])
def test_known_dead_addresses_do_not_come_back(dead):
    """Мёртвый адрес легко вернуть копипастой из старого конфига."""
    urls = " ".join(s.url or "" for s in _cfg.load().sources)
    assert dead not in urls, f"вернулся мёртвый адрес: {dead}"


def test_every_source_has_a_plausible_address():
    """Дешёвая страховка от опечатки в каталоге: адрес должен быть http(s)
    и без пробелов.

    Проверяем только тех, кто реально ходит по сети. У `tavily_fallback`
    адрес псевдо-схемы `tavily://search` — это внутренний коннектор, а не
    лента, и требовать от него http бессмысленно.
    """
    over_http = {"rss", "html", "edgar", "edisclosure", "playwright_rss"}
    for s in _cfg.load().sources:
        if s.connector not in over_http:
            continue
        url = s.url or ""
        assert re.match(r"^https?://\S+$", url), f"{s.id}: подозрительный адрес {url!r}"


def test_html_sources_are_not_judged_by_rss_rules():
    """⚠️ НА ЭТОМ Я САМ ОШИБСЯ. Скормил страницу Минфина разборщику лент и
    объявил источник сломанным («битый XML»), хотя у него коннектор `html` и
    он исправно отдаёт 18 записей за неделю. Проверять источник надо ЕГО
    коннектором, а не тем, который под рукой."""
    minfin = next((s for s in _cfg.load().sources if s.id == "minfin_ru"), None)
    assert minfin is not None
    assert minfin.connector == "html", (
        "у Минфина сменился коннектор — прежняя диагностика опиралась на html")
