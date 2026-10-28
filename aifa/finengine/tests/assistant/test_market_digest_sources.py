"""Сценарий не обходит запрет сети и не маскирует отказы источников."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from finengine.llm.openclaw import set_web_search, web_search_allowed
from finengine.skills.news.classify import _parse_response
from finengine.skills.news.config import NewsConfig, SourceConfig
from finengine.skills.news.market import DigestRequest, run_bounded
from finengine.skills.news.models import ClassifiedItem, NewsItem
from finengine.skills.news.service import ServiceDigest

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
FEED = """<rss version="2.0"><channel><title>ЦБ</title><item><title>Ставка ЦБ</title>
<link>https://feed.example/news/1</link><pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate>
<description>Ставка без изменений</description></item></channel></rss>"""


def request(stopped=lambda: False):
    return DigestRequest(NOW - timedelta(hours=168), NOW, stopped, lambda _: None, "trace-1", "agent-1")


def catalog(*ids):
    return NewsConfig(sources=[SourceConfig(
        id=sid, display_name=sid, type="official", region="RU", connector="rss",
        url=f"https://feed.example/{sid}", weight=1.0,
    ) for sid in ids], entities=[])


async def classified(items, **kwargs):
    return [ClassifiedItem(item, True, "regulator", "RU", 0.8, item.title) for item in items], {"in": 10, "out": 5, "cost_usd": 0.01}


async def no_macro(client):
    return {}, []


@pytest.fixture(autouse=True)
def network_choice():
    previous = web_search_allowed()
    set_web_search(True)
    yield
    set_web_search(previous)


async def test_network_is_refused_before_the_first_request():
    calls = []
    transport = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, text=FEED))
    set_web_search(False)
    with pytest.raises(ValueError, match="выключен"):
        await ServiceDigest(transport=transport, catalog=catalog("feed")) (request())
    assert not calls


async def test_catalogue_feed_carries_trace_and_fixed_window():
    calls = []
    transport = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, text=FEED))
    service = ServiceDigest(transport=transport, catalog=catalog("feed"), classifier=classified, macro_loader=no_macro)
    result = await service(request())
    assert result.status == "complete"
    assert result.window_end == NOW
    assert result.tokens_in == 10 and result.cost_usd == 0.01
    assert calls[0].headers["X-Trace-Id"] == "trace-1"
    assert calls[0].headers["X-Agent-Id"] == "agent-1"
    assert result.news[0]["url"] == "https://feed.example/news/1"


async def test_redirect_outside_catalogue_is_never_requested():
    calls = []
    transport = httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}))
    with pytest.raises(ValueError, match="Все источники"):
        await ServiceDigest(transport=transport, catalog=catalog("feed"))(request())
    assert len(calls) == 1


async def test_source_failure_makes_useful_digest_partial():
    transport = httpx.MockTransport(lambda req: httpx.Response(503) if req.url.path == "/bad" else httpx.Response(200, text=FEED))
    result = await ServiceDigest(
        transport=transport, catalog=catalog("feed", "bad"), classifier=classified, macro_loader=no_macro,
    )(request())
    assert result.status == "partial"
    assert "bad" in " ".join(result.warnings)


async def test_empty_success_is_distinct_from_source_outage():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<rss><channel/></rss>"))
    result = await ServiceDigest(transport=transport, catalog=catalog("feed"), macro_loader=no_macro)(request())
    assert result.status == "empty"


async def test_cancelled_request_does_not_return_a_late_success():
    async def expensive(req):
        import asyncio
        await asyncio.sleep(10)
        raise AssertionError("не должен завершиться")
    with pytest.raises(RuntimeError, match="остановлен"):
        await run_bounded(expensive, request(lambda: True))


def test_valid_json_with_missing_classification_is_an_error():
    item = NewsItem("ЦБ", "feed", "official", "RU", 1.0, "Ставка", "", "https://feed.example", NOW, "hash")
    with pytest.raises(ValueError):
        _parse_response('{"results": []}', [item], strict_complete=True)


async def test_html_challenge_is_a_source_outage_not_empty_digest():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="<html>Just a moment... Access denied</html>"))
    with pytest.raises(ValueError, match="Все источники"):
        await ServiceDigest(transport=transport, catalog=catalog("feed"), macro_loader=no_macro)(request())


def test_macro_state_is_local_to_each_digest_event_loop(monkeypatch):
    import asyncio
    from finengine.skills.news import macro_indicators as macro
    specs = [s for s in macro._REGISTRY if s.provider == "investing"][:2]
    monkeypatch.setattr(macro, "_REGISTRY", specs)
    monkeypatch.setattr(macro, "_INVESTING_DELAY", 0)
    states = []
    async def fetch(client, spec, *, trip_state=None):
        states.append(trip_state)
        assert trip_state is not macro._investing_trip_state
        await asyncio.sleep(0.001)
        return [{"date": "2026-10-01", "value": 50.0}]
    monkeypatch.setattr(macro, "_fetch_investing", fetch)
    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            return await ServiceDigest()._macro(client)
    first, second = asyncio.run(collect()), asyncio.run(collect())
    assert len(first[0]) == len(second[0]) == 2
    assert first[1] == second[1] == []
    assert states[0] is states[1] and states[2] is states[3] and states[0] is not states[2]


async def test_browser_redirect_is_validated_before_the_next_request():
    from types import SimpleNamespace
    from finengine.skills.news.service import BrowserRoute
    calls = []
    class Route:
        request = SimpleNamespace(url="https://feed.example/rss")
        async def fetch(self, **kwargs):
            calls.append(kwargs)
            return Response()
        async def abort(self):
            calls.append("abort")
        async def fulfill(self, **kwargs):
            raise AssertionError("Unapproved redirect must never be fulfilled")
    class Response:
        status = 302
        headers = {"location": "http://127.0.0.1/private"}
        async def dispose(self):
            pass
    await BrowserRoute(frozenset({"feed.example"}), request())(Route())
    assert calls == [{"url": "https://feed.example/rss", "max_redirects": 0, "timeout": 25000}, "abort"]


async def test_running_http_is_cancelled_and_released():
    import asyncio
    import threading
    stopped, entered, released = threading.Event(), threading.Event(), threading.Event()
    async def fetch(req):
        entered.set()
        try:
            await asyncio.sleep(10)
            return httpx.Response(200, text=FEED)
        finally:
            released.set()
    service = ServiceDigest(transport=httpx.MockTransport(fetch), catalog=catalog("feed"), macro_loader=no_macro)
    task = asyncio.create_task(run_bounded(service, request(stopped.is_set)))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    stopped.set()
    with pytest.raises(RuntimeError, match="остановлен"):
        await task
    assert released.is_set()


async def test_unconfigured_macro_provider_is_not_a_source_outage(monkeypatch):
    from finengine.config import settings
    from finengine.skills.news import macro_indicators as macro
    monkeypatch.setattr(settings, "fred_api_key", "")
    monkeypatch.setattr(settings, "eia_api_key", "")
    monkeypatch.setattr(macro, "_REGISTRY", [s for s in macro._REGISTRY if s.provider in {"fred", "eia"}][:3])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=FEED))
    result = await ServiceDigest(transport=transport, catalog=catalog("feed"), classifier=classified)(request())
    assert result.status == "complete"
    assert "не настроен ключ" in " ".join(result.warnings)


async def test_classifier_gets_one_retry_for_a_malformed_answer():
    attempts = []

    async def flaky(items, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("region вне списка")
        return await classified(items, **kwargs)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=FEED))
    result = await ServiceDigest(transport=transport, catalog=catalog("feed"), classifier=flaky, macro_loader=no_macro)(request())
    assert result.status == "complete" and len(attempts) == 2

    async def broken(items, **kwargs):
        raise ValueError("region вне списка")

    with pytest.raises(ValueError):
        await ServiceDigest(transport=transport, catalog=catalog("feed"), classifier=broken, macro_loader=no_macro)(request())


def test_macro_fetchers_stay_inside_the_network_boundary():
    from urllib.parse import urlsplit
    from finengine.skills.news import macro_indicators as macro
    from finengine.skills.news.service import MACRO_HOSTS
    bases = [macro._FRED_BASE, macro._EUROSTAT_BASE, macro._CBR_INFL_URL, macro._EIA_BASE, macro._NBS_BASE, macro._INVESTING_BASE]
    assert {urlsplit(base).hostname for base in bases} <= MACRO_HOSTS
    # Новый провайдер в реестре обязан получить хост здесь и в MACRO_HOSTS, иначе граница отрежет его запросы.
    assert {s.provider for s in macro._REGISTRY} <= {"fred", "eurostat", "cbr_scrape", "eia", "nbs", "investing"}


async def test_classifier_outage_names_the_provider_code_without_leaking_the_body():
    async def outage(items, **kwargs):
        return [], {"error": "Error code: 402 - {'error': {'message': 'keys/1d7a3ab8 limit'}}"}

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=FEED))
    with pytest.raises(ValueError) as failure:
        await ServiceDigest(transport=transport, catalog=catalog("feed"), classifier=outage, macro_loader=no_macro)(request())
    assert "402" in str(failure.value) and "1d7a3ab8" not in str(failure.value)

