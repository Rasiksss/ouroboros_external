"""RSS/Atom connector built on `feedparser`.

Handles the common gotchas:
  * dates: feedparser gives a `struct_time` in UTC; we convert to tz-aware
  * snippets: most feeds put HTML in `summary` — strip tags, collapse ws
  * malformed feeds: feedparser returns `bozo=1` but usually still parses
  * old items: filter by `since_hours` against published/updated
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from finengine.net.ru_gov_tls import verify_for
from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_google_news_publisher(title: str) -> str:
    """Убрать хвост «- Издание», который Google News вешает на каждый заголовок.

    ЗАЧЕМ (замер на боевом проде 17.08). Google News отдаёт заголовок в виде
    «Заголовок - Издание», и хвост ехал в выпуск дословно:
        'NBPE Announces Transaction in Own Shares - GlobeNewswire'
    У 26 записей из 229 заголовка не было вовсе, оставался голый '- GlobeNewswire'
    — за 48 часов таких набралось 146 по всем лентам Google News. Дедуп их не
    ловит: формально заголовки разные.

    Издание не теряется — оно и так едет в `source`/`display_name` и попадает в
    выпуск отдельным полем. В заголовке это шум.

    Пустой результат = отбраковка: новость без заголовка показывать нечего, а
    имя агентства вместо него хуже, чем её отсутствие.
    """
    t = title.strip()
    # Заголовок, начинающийся с тире, — это хвост издания без самого заголовка
    # («- GlobeNewswire»). Разбирать там нечего. Проверка отдельная, потому что
    # разделителем служит « - » С ПРОБЕЛОМ СЛЕВА, а здесь его нет — на этом
    # первая редакция правки и прошла мимо 26 записей из 229.
    if t.startswith(("-", "–", "—")):
        return ""
    head = t.rsplit(" - ", 1)[0].strip() if " - " in t else t
    return "" if len(head) < 3 else head


def _clean_html(s: str) -> str:
    """Strip HTML tags + decode entities + collapse whitespace."""
    if not s:
        return ""
    s = html.unescape(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _entry_datetime(entry: Any) -> datetime | None:
    """feedparser gives us `published_parsed` / `updated_parsed` as
    `time.struct_time` in UTC. Pick whichever exists, return tz-aware."""
    import time
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


class RSSConnector:
    """RSS/Atom feed fetcher. One instance per source."""

    def __init__(self, source: SourceConfig):
        self.source = source

    def fetch(self, *, since_hours: int = 48, limit: int = 50) -> list[NewsItem]:
        try:
            import feedparser
        except ImportError:
            log.error("feedparser not installed — RSS sources skipped")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        # Fetch via httpx so we control SSL handling + User-Agent + timeout
        # (feedparser's urllib defaults break on macOS dev w/o certifi
        # and don't set a sane User-Agent).
        try:
            import httpx
            # Browser-like UA + relaxed Accept — many wires (BLS, Kommersant,
            # GlobeNewswire, BusinessWire) reject the "compatible; bot" form
            # outright. A real-Chrome UA gets through without changing
            # behaviour upstream.
            r = httpx.get(
                self.source.url,
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/121.0.0.0 Safari/537.36",
                    "Accept": "application/rss+xml, application/atom+xml, "
                              "application/xml;q=0.9, text/xml;q=0.9, "
                              "text/html;q=0.8, */*;q=0.5",
                    "Accept-Language": "en-US,en;q=0.7,ru;q=0.3",
                    # 🔴 БЕЗ `br`. Просить brotli, не умея его развернуть, —
                    # это тихая порча тела ответа: сервер честно отдаёт
                    # `Content-Encoding: br`, httpx без пакета `brotli` кладёт
                    # в `r.content` сжатые байты, и feedparser сообщает
                    # «bozo=1, not well-formed (invalid token)» — то есть
                    # источник выглядит СЛОМАННЫМ, хотя он исправен.
                    # Замер на проде 18.08: TechCrunch и MarketWatch падали
                    # именно так, тот же запрос без `br` отдаёт валидный XML.
                    # Ставить зависимость ради этого не нужно: gzip и deflate
                    # httpx разворачивает сам, а их поддерживают все ленты.
                    "Accept-Encoding": "gzip, deflate",
                },
                # Ленты госсайтов РФ (Росстат) подписаны центром Минцифры,
                # которого нет в штатном хранилище: замер 18.08.2026 —
                # `[SSL: CERTIFICATE_VERIFY_FAILED]`, ноль записей, источник
                # выглядит недоступным. `verify_for` расширяет доверие ТОЛЬКО
                # для закрытого списка адресов в net/ru_gov_tls.py, всем
                # остальным (openrouter, tavily, google news) возвращает
                # штатную проверку. Модуль лежал в репозитории с 17.08 без
                # единого боевого вызова — правку объявили сделанной, а до
                # сборщика она не доехала.
                verify=verify_for(self.source.url),
            )
            if r.status_code != 200:
                log.warning("RSS %s: HTTP %d", self.source.id, r.status_code)
                return []
            body = r.content  # bytes — let feedparser detect encoding from XML decl
        except Exception as e:  # noqa: BLE001 — net errors, SSL, DNS, timeout
            log.warning("RSS fetch failed for %s: %s", self.source.id, e)
            return []

        try:
            parsed = feedparser.parse(body)
        except Exception as e:  # noqa: BLE001 — malformed XML
            log.warning("RSS parse failed for %s: %s", self.source.id, e)
            return []

        if getattr(parsed, "bozo", 0) and not parsed.entries:
            log.warning("RSS feed %s: bozo=1 with 0 entries (%s)",
                        self.source.id, getattr(parsed, "bozo_exception", "?"))
            return []

        out: list[NewsItem] = []
        is_google_news = "news.google.com" in (self.source.url or "")
        for entry in parsed.entries[:limit]:
            title = _clean_html(getattr(entry, "title", "") or "")
            if is_google_news:
                title = _strip_google_news_publisher(title)
            if not title:
                continue
            link = (getattr(entry, "link", "") or "").strip()
            if not link:
                continue

            published = _entry_datetime(entry)
            if published is None:
                # No date → skip, can't tell if fresh
                continue
            if published < cutoff:
                continue

            # Snippet candidates in order of preference.
            snippet = (
                _clean_html(getattr(entry, "summary", ""))
                or _clean_html(getattr(entry, "description", ""))
                or _clean_html(
                    (entry.get("content") or [{}])[0].get("value", "")
                    if isinstance(entry.get("content"), list) else ""
                )
            )
            # Cap snippet — full article body would inflate context downstream.
            if len(snippet) > 600:
                snippet = snippet[:597] + "…"

            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type=self.source.type,  # type: ignore[arg-type]
                region=self.source.region,  # type: ignore[arg-type]
                source_weight=self.source.weight,
                title=title,
                snippet=snippet,
                url=link,
                published_at=published,
                raw_hash=canonical_hash(link, title),
            ))

        log.info("RSS %s: fetched %d items (window %dh)",
                 self.source.id, len(out), since_hours)
        return out
