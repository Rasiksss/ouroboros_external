"""Поиск в сети ходит через прокси для зарубежных API (17.08.2026).

ЗАМЕР НА БОЕВОМ ПРОДЕ. С этого VPS исходящий трафик фильтруется по хостам:
    api.tavily.com  → 403 (HTML-страница nginx, а не ответ Tavily)
    openrouter.ai   → 403
    news.google.com → 302      www.cbr.ru → 200
Через прокси, которым уже ходит макро-блок, api.tavily.com отвечает 200 с
настоящими результатами. То есть поиск отбивала СЕТЬ, а не Tavily и не ключ.

⚠️ Сначала я поставил неверный диагноз «ключа нет» — проверял переменную под
чужим именем. Ключ был на месте всё время, и подмена его новым ничего не
изменила: 403 остался. Сторож закрывает именно ту причину, что подтвердилась
замером.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.skills import web_search as ws


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FINENGINE_MACRO_FOREIGN_PROXY", raising=False)
    monkeypatch.setenv("FINENGINE_TAVILY_API_KEY", "test-key")


def test_proxy_is_read_from_the_existing_variable(monkeypatch):
    """Переменная ПЕРЕИСПОЛЬЗУЕТСЯ, а не заводится новая: она уже прописана на
    обоих стендах. Новую пришлось бы раскладывать руками, и до первой выкатки
    поиск снова молчал бы."""
    monkeypatch.setenv("FINENGINE_MACRO_FOREIGN_PROXY", "http://proxy:8888")
    assert ws.foreign_proxy() == "http://proxy:8888"


def test_absent_proxy_means_direct_call_not_a_crash():
    """На машине без фильтрации прокси не нужен — поиск обязан работать
    напрямую, а не падать на отсутствии переменной."""
    assert ws.foreign_proxy() is None


def test_blank_proxy_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("FINENGINE_MACRO_FOREIGN_PROXY", "   ")
    assert ws.foreign_proxy() is None


def test_search_actually_passes_the_proxy_to_the_client(monkeypatch):
    """ГЛАВНАЯ ПРОВЕРКА — цепочка целиком. Отрицательный контроль: убрал
    `proxy=` из вызова клиента — краснеет.

    Проверять один лишь `foreign_proxy()` было бы бесполезно: ровно на разрыве
    «значение есть, но до вызова не доехало» мы горели в #252 и #253.
    """
    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "T", "url": "u", "content": "c"}]}

    class _Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):  # noqa: A002
            seen["url"] = url
            seen["payload"] = json
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setenv("FINENGINE_MACRO_FOREIGN_PROXY", "http://proxy:8888")

    out = asyncio.run(ws.web_search("тест", max_results=1))
    assert seen["proxy"] == "http://proxy:8888", "прокси не доехал до клиента"
    assert seen["url"] == "https://api.tavily.com/search"
    assert seen["payload"]["api_key"] == "test-key"
    assert len(out) == 1 and out[0].title == "T"


def test_no_key_short_circuits_without_network(monkeypatch):
    """Без ключа наружу не ходим вовсе."""
    monkeypatch.delenv("FINENGINE_TAVILY_API_KEY", raising=False)
    assert asyncio.run(ws.web_search("тест")) == []
    assert ws.is_enabled() is False
