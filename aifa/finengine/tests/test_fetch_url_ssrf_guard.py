"""`fetch_url` (`llm/tools.py::_h_fetch_url`) — найденная в Срезе 7 SSRF-дыра.

ДО ЭТОЙ СЕССИИ: `client.get(url, follow_redirects=True)` шёл по любому адресу,
куда бы сервер ни перенаправил, без единой проверки хоста — localhost,
приватные сети, облачный metadata-эндпоинт включительно. Фикс переиспользует
ТУ ЖЕ проверку, что уже защищает `document_acquisition` (Срез 7):
`url_safety.validate_url_for_fetch` на исходном URL и на КАЖДОМ redirect hop.

Мокается `httpx.AsyncClient` целиком — тот же приём, что уже использует
`tests/test_web_search_goes_through_the_proxy.py` (respx в зависимостях нет)."""
from __future__ import annotations

import asyncio
import json
import socket

import httpx

from finengine.llm.tools import ToolContext, _h_fetch_url


class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, text="", is_redirect=False, url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.is_redirect = is_redirect
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]


def _fake_client_factory(responses_by_url: dict[str, _FakeResponse], calls: list[str]):
    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            try:
                return responses_by_url[url]
            except KeyError:
                raise AssertionError(f"неожиданный запрос к {url}; известны: {list(responses_by_url)}") from None

    return _FakeClient


def _patch_dns(monkeypatch, host_to_ip: dict[str, str]):
    """`_h_fetch_url` не принимает `resolver` параметром (это `url_safety`
    предлагает атомам через DI, а сам тул зовёт `validate_url_for_fetch` без
    переопределения) — значит подменять нужно НАСТОЯЩУЮ точку вызова
    (`socket.getaddrinfo`), а не аргумент по умолчанию: он захвачен один раз
    при определении функции, monkeypatch модульного имени `_default_resolver`
    на него уже не повлияет."""
    def _fake_getaddrinfo(host, port, *a, **kw):
        if host not in host_to_ip:
            raise socket.gaierror(f"нет такого хоста в тестовом DNS: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host_to_ip[host], port))]
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


async def _call(args: dict) -> dict:
    return json.loads(await _h_fetch_url(args, ToolContext(user_id=1)))


def test_successful_fetch_still_works(monkeypatch):
    calls: list[str] = []
    responses = {
        "https://example.com/page": _FakeResponse(
            status_code=200, headers={"content-type": "text/html"},
            text="<html><body><p>hello</p></body></html>", url="https://example.com/page",
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(responses, calls))
    _patch_dns(monkeypatch, {"example.com": "93.184.216.34"})
    out = asyncio.run(_call({"url": "https://example.com/page"}))
    assert "hello" in out["text"]
    assert out["status"] == 200
    assert calls == ["https://example.com/page"]


def test_initial_url_pointing_at_private_ip_is_rejected_before_any_request(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory({}, calls))
    out = asyncio.run(_call({"url": "http://127.0.0.1:6379/"}))
    assert "error" in out
    assert not calls, "не должен был дойти до HTTP-запроса — SSRF-проверка обязана остановить раньше"


def test_cloud_metadata_ip_is_rejected(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory({}, calls))
    out = asyncio.run(_call({"url": "http://169.254.169.254/latest/meta-data/"}))
    assert "error" in out
    assert not calls


def test_redirect_to_private_address_is_rejected_not_silently_followed(monkeypatch):
    """Главный регресс-случай: раньше `follow_redirects=True` увёл бы запрос
    на приватный адрес молча."""
    calls: list[str] = []
    responses = {
        "https://safe.example.com/start": _FakeResponse(
            status_code=302, is_redirect=True, headers={"location": "http://169.254.169.254/"},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(responses, calls))
    _patch_dns(monkeypatch, {"safe.example.com": "93.184.216.34"})
    out = asyncio.run(_call({"url": "https://safe.example.com/start"}))
    assert "error" in out
    assert calls == ["https://safe.example.com/start"], "должен был остановиться ПОСЛЕ первого запроса, не дойти до редиректа"


def test_redirect_to_safe_host_is_still_followed(monkeypatch):
    calls: list[str] = []
    responses = {
        "https://old.example.com/": _FakeResponse(
            status_code=302, is_redirect=True, headers={"location": "https://new.example.com/"},
        ),
        "https://new.example.com/": _FakeResponse(
            status_code=200, headers={"content-type": "text/html"},
            text="<html><body>moved</body></html>", url="https://new.example.com/",
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(responses, calls))
    _patch_dns(monkeypatch, {"old.example.com": "93.184.216.34", "new.example.com": "93.184.216.35"})
    out = asyncio.run(_call({"url": "https://old.example.com/"}))
    assert "moved" in out["text"]
    assert calls == ["https://old.example.com/", "https://new.example.com/"]


def test_too_many_redirects_is_reported_not_looped_forever(monkeypatch):
    calls: list[str] = []
    responses = {
        f"https://h{i}.example.com/": _FakeResponse(
            status_code=302, is_redirect=True, headers={"location": f"https://h{i + 1}.example.com/"},
        )
        for i in range(10)
    }
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory(responses, calls))
    _patch_dns(monkeypatch, {f"h{i}.example.com": "93.184.216.34" for i in range(10)})
    out = asyncio.run(_call({"url": "https://h0.example.com/"}))
    assert "error" in out
    assert len(calls) <= 6  # 5 редиректов + исходный запрос, не бесконечно


def test_userinfo_obfuscation_is_rejected(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client_factory({}, calls))
    out = asyncio.run(_call({"url": "http://example.com@169.254.169.254/"}))
    assert "error" in out
    assert not calls
