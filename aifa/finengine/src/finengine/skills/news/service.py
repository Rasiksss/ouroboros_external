"""Сбор из серверного каталога для сценария службы, без старого чатового состояния."""
from __future__ import annotations

import asyncio
import calendar
import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol

from finengine.contracts.digest import DigestNews

import feedparser
import httpx
from bs4 import BeautifulSoup

from finengine.config import settings
from finengine.contracts import DigestOutput
from finengine.llm.openclaw import web_search_allowed
from finengine.llm.openrouter import request_headers
from finengine.skills.news import classify, config, filter, macro_indicators as macro, rank, render
from finengine.net.ru_gov_tls import verify_for
from finengine.skills.news.connectors.edgar import _UA as EDGAR_USER_AGENT
from finengine.skills.news.connectors.edisclosure import _parse_dt, _parse_rows
from finengine.skills.news.connectors.html import _EXTRACTORS
from finengine.skills.news.connectors.rss import _clean_html, _strip_google_news_publisher
from finengine.skills.news.market import DigestRequest
from finengine.skills.news.models import ClassifiedItem, NewsItem, canonical_hash

if TYPE_CHECKING:
    from playwright.async_api import Route, WebSocketRoute


class Classifier(Protocol):
    async def __call__(self, items: list[NewsItem], *, strict_complete: bool) -> tuple[list[ClassifiedItem], dict]: ...


MacroLoader = Callable[[httpx.AsyncClient], Awaitable[tuple[dict, list[str]]]]

MACRO_HOSTS = frozenset({
    "api.stlouisfed.org", "ec.europa.eu", "www.cbr.ru", "www.eia.gov", "api.eia.gov",
    "data.stats.gov.cn", "www.investing.com",
})


def catalogue_hosts(catalog: config.NewsConfig | None = None) -> tuple[str, ...]:
    sources = (catalog or config.load()).active_sources()
    return tuple(sorted(MACRO_HOSTS | {host for s in sources if (host := urlsplit(s.url).hostname)}))


def _classification_failure(usage: dict) -> str:
    """Причина отказа классификатора для агента: код ответа провайдера, без тела и адресов с идентификаторами ключей."""
    code = re.search(r"Error code: (\d{3})", str(usage.get("error", "")))
    reason = f"провайдер модели ответил кодом {code[1]}" if code else "модель не вернула результат"
    return f"Классификация дайджеста не выполнена: {reason}; роль пользователя и параметры запроса здесь ни при чём"


# Те же заголовки, что у исходного RSS-коннектора: часть лент отвечает ботовому User-Agent отказом или не-лентой.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.7,ru;q=0.3",
    "Accept-Encoding": "gzip, deflate",
}


def _unconfigured_ids() -> frozenset[str]:
    """Показатели, чьему провайдеру не выдан ключ: это настройка службы, а не отказ источника."""
    missing = {name for name, key in (("fred", settings.fred_api_key), ("eia", settings.eia_api_key)) if not key}
    return frozenset(s.id for s in macro._REGISTRY if s.provider in missing)


def catalogue_version() -> str:
    raw = json.dumps([dataclasses.asdict(s) for s in config.load().active_sources()], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class SourceBoundary:
    hosts: frozenset[str]
    request: DigestRequest

    async def __call__(self, outgoing: httpx.Request) -> None:
        if outgoing.url.host not in self.hosts or outgoing.url.scheme not in {"http", "https"}:
            raise ValueError("Источник или перенаправление вне серверного каталога")
        if self.request.stopped():
            raise asyncio.CancelledError
        if self.request.trace_id:
            outgoing.headers["X-Trace-Id"] = self.request.trace_id
        if self.request.agent_id:
            outgoing.headers["X-Agent-Id"] = self.request.agent_id


@dataclasses.dataclass(frozen=True)
class BrowserRoute:
    hosts: frozenset[str]
    request: DigestRequest

    async def __call__(self, route: Route) -> None:
        current = route.request.url
        for _ in range(10):
            parsed = urlsplit(current)
            if parsed.hostname not in self.hosts or parsed.scheme not in {"http", "https"} or self.request.stopped():
                await route.abort()
                return
            response = await route.fetch(url=current, max_redirects=0, timeout=25000)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.dispose()
                if not location:
                    await route.abort()
                    return
                current = urljoin(current, location)
                continue
            await route.fulfill(response=response)
            await response.dispose()
            return
        await route.abort()


async def close_websocket(socket: WebSocketRoute) -> None:
    await socket.close()


class ServiceDigest:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 catalog: config.NewsConfig | None = None, classifier: Classifier | None = None,
                 macro_loader: MacroLoader | None = None) -> None:
        self.transport = transport
        self.catalog = catalog
        self.classifier = classifier or classify.classify_batch
        self.macro_loader = macro_loader or self._macro

    async def __call__(self, request: DigestRequest) -> DigestOutput:
        if not web_search_allowed():
            raise ValueError("В этом ходе доступ к внешним источникам выключен")
        cfg = self.catalog or config.load()
        hosts = frozenset(catalogue_hosts(cfg))
        async with self._http(hosts, request) as client:
            request.progress({"type": "progress", "text": "Собираю дайджест из серверного каталога"})
            sources = cfg.active_sources()
            if not sources:
                raise ValueError("В серверном каталоге нет включённых источников")
            slots, sec_slots = asyncio.Semaphore(3), asyncio.Semaphore(3)
            results = await asyncio.gather(
                *(self._fetch(client, s, request, hosts, slots, sec_slots) for s in sources), return_exceptions=True,
            )
            failures = {s.id: type(value).__name__ for s, value in zip(sources, results) if isinstance(value, BaseException)}
            if len(failures) == len(sources):
                raise ValueError("Все источники дайджеста недоступны")
            pool = {item.raw_hash: item for value in results if isinstance(value, list) for item in value}
            items = list(pool.values())
            filter.annotate(items, cfg)
            candidates = rank.top_n(filter.dedup_near_duplicates(filter.filter_relevant(items, cfg)), n=settings.news_top_candidates)
            if candidates:
                with request_headers(request.trace_id, request.agent_id):
                    try:
                        classified, usage = await self.classifier(candidates, strict_complete=True)
                    except ValueError:
                        # Одна ошибочная строка ответа не должна стоить уже собранного выпуска; повтор один.
                        request.progress({"type": "progress", "text": "Классификатор вернул неполный ответ, повторяю"})
                        classified, usage = await self.classifier(candidates, strict_complete=True)
                if usage.get("error") or not classified:
                    raise ValueError(_classification_failure(usage))
            else:
                classified, usage = [], {}
            payload, macro_failures = await self.macro_loader(client)
        now = request.window_end.astimezone(ZoneInfo("Europe/Moscow"))
        markdown, _ = render.render_digest(
            classified, cfg=cfg, now=now, macro_payload=payload, seen_hashes=set(), macro_mode="full", archive_items=[],
        )
        warnings = [f"Недоступны новостные источники: {', '.join(sorted(failures))}"] if failures else []
        if macro_failures:
            warnings.append("Недоступны макропоказатели: " + ", ".join(macro_failures))
        if skipped := sorted(_unconfigured_ids()):
            warnings.append("Макропоказатели пропущены: не настроен ключ провайдера — " + ", ".join(skipped))
        warnings.append("Источники возвращают ограниченные ленты; полнота архива за период не гарантируется")
        partial = bool(failures or macro_failures)
        if partial and not payload and not any(c.is_relevant for c in classified):
            raise ValueError("Из-за отказов источников нет данных для полезного выпуска")
        status: Literal["complete", "partial", "empty"] = "partial" if partial else "complete" if any(c.is_relevant for c in classified) else "empty"
        window = f"Окно новостей: {request.window_start.isoformat()} — {request.window_end.isoformat()}"
        markdown = f"{markdown}\n\n{window}\n\nСтатус: {status}\n\n" + "\n".join(warnings)
        news: list[DigestNews] = [
            {"title": c.item.title, "summary": c.one_sentence_ru, "url": c.item.url,
             "published_at": c.item.published_at.isoformat(), "source_id": c.item.source_id, "category": c.category}
            for c in classified if c.is_relevant
        ]
        return DigestOutput(
            window_start=request.window_start, window_end=request.window_end, collected_at=datetime.now(timezone.utc),
            markdown=markdown, status=status, news=news, macro=payload, warnings=warnings,
            source_stats={"total": len(sources), "failed": len(failures), "raw": len(items), "candidates": len(candidates)},
            tokens_in=int(usage.get("in", 0)), tokens_out=int(usage.get("out", 0)), cost_usd=float(usage.get("cost_usd", 0)),
        )

    def _http(self, hosts: frozenset[str], request: DigestRequest, *, verify=True) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, timeout=25, follow_redirects=True, verify=verify,
            event_hooks={"request": [SourceBoundary(hosts, request)]}, limits=httpx.Limits(max_connections=12),
            headers=BROWSER_HEADERS,
        )

    async def _get(self, client, url: str, connector: str, hosts: frozenset[str], request: DigestRequest,
                   sec_slots: asyncio.Semaphore) -> httpx.Response:
        if connector == "edgar":
            # SEC отказывает без контактного User-Agent и ограничивает 10 запросов в секунду.
            async with sec_slots:
                response = await client.get(url, headers={"User-Agent": EDGAR_USER_AGENT})
                await asyncio.sleep(0.1)
                return response
        verify = verify_for(url)
        if verify is True:
            return await client.get(url)
        async with self._http(hosts, request, verify=verify) as special:  # государственные адреса: центры Минцифры
            return await special.get(url)

    async def _fetch(self, client, source, request: DigestRequest, hosts: frozenset[str], slots: asyncio.Semaphore,
                     sec_slots: asyncio.Semaphore) -> list[NewsItem]:
        url = source.url
        if source.connector == "telegram":
            url = "https://t.me/s/" + url.rstrip("/").split("/")[-1]
        if source.connector in {"edisclosure", "playwright_rss"}:
            async with slots:
                body = await self._browser(url, request, hosts)
        else:
            response = await self._get(client, url, source.connector, hosts, request, sec_slots)
            response.raise_for_status()
            body = response.text
        records: list[tuple[str, str, datetime, str]] = []
        if source.connector in {"rss", "edgar", "playwright_rss"}:
            parsed = feedparser.parse(body)
            if not parsed.version or (parsed.bozo and not parsed.entries):
                raise ValueError("Неверный формат ленты")
            for entry in parsed.entries[:50]:
                stamp = entry.get("published_parsed") or entry.get("updated_parsed")
                if not stamp:
                    continue
                title = _clean_html(entry.get("title", ""))
                if "news.google.com" in source.url:
                    title = _strip_google_news_publisher(title)
                records.append((title, entry.get("link", ""), datetime.fromtimestamp(calendar.timegm(stamp), timezone.utc), _clean_html(entry.get("summary", ""))))
        elif source.connector == "html":
            extractor = _EXTRACTORS.get(source.id)
            if extractor is None:
                raise ValueError("Для HTML-источника нет парсера")
            for title, link, stamp in extractor(body, source.url):
                if stamp is not None:
                    records.append((title, link, min(stamp, request.window_end), title))
        elif source.connector == "edisclosure":
            for stamp, _, title, link in _parse_rows(body):
                date = _parse_dt(stamp)
                if date:
                    records.append((title, urljoin(url, link), date, title))
        elif source.connector == "telegram":
            soup = BeautifulSoup(body, "html.parser")
            for message in soup.select("div.tgme_widget_message"):
                date_tag, text = message.find("time"), message.select_one(".tgme_widget_message_text")
                if date_tag and date_tag.get("datetime") and text:
                    stamp = datetime.fromisoformat(str(date_tag["datetime"]))
                    if stamp.tzinfo is not None:
                        content = text.get_text(" ", strip=True)
                        records.append((content[:200], "https://t.me/" + str(message.get("data-post", "")), stamp, content))
        else:
            raise ValueError("Неизвестный коннектор серверного каталога")
        return [NewsItem(
            source=source.display_name, source_id=source.id, source_type=source.type, region=source.region,
            source_weight=source.weight, title=title, url=link, published_at=stamp, snippet=snippet[:600],
            raw_hash=canonical_hash(link, title),
        ) for title, link, stamp, snippet in records if title and link and request.window_start <= stamp <= request.window_end]

    async def _browser(self, url: str, request: DigestRequest, hosts: frozenset[str]) -> str:
        from playwright.async_api import async_playwright  # необязательная браузерная зависимость

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, timeout=25000, args=["--no-sandbox"])
            try:
                context = await browser.new_context(service_workers="block", extra_http_headers={
                    **({"X-Trace-Id": request.trace_id} if request.trace_id else {}),
                    **({"X-Agent-Id": request.agent_id} if request.agent_id else {}),
                })
                page = await context.new_page()
                await context.route("**/*", BrowserRoute(hosts, request))
                await context.route_web_socket("**/*", close_websocket)
                await page.goto(url, wait_until="networkidle", timeout=25000)
                return await page.content()
            finally:
                await asyncio.wait_for(browser.close(), timeout=5)

    async def _macro(self, client: httpx.AsyncClient) -> tuple[dict, list[str]]:
        investing_sem = asyncio.Semaphore(1)
        investing_state = {"streak": 0, "tripped": False}
        specs = [s for s in macro._REGISTRY if s.id not in _unconfigured_ids()]
        results = await asyncio.gather(*(self._macro_indicator(client, s, investing_sem, investing_state) for s in specs), return_exceptions=True)
        return (
            {s.id: result for s, result in zip(specs, results) if isinstance(result, dict) and result},
            [s.id for s, result in zip(specs, results) if not isinstance(result, dict) or not result],
        )

    async def _macro_indicator(self, client, spec, investing_sem, investing_state) -> dict:
        obs = await macro._dispatch_fetch(client, spec, investing_sem=investing_sem, investing_state=investing_state)
        dynamics = macro._compute_dynamics(spec, obs)
        if not dynamics:
            return {}
        return {**dynamics, "id": spec.id, "ru_name": spec.ru_name, "region": spec.region,
                "unit": spec.unit, "source_url": macro.source_link(spec), "published": "",
                "fetched_at": datetime.now(timezone.utc).timestamp()}
