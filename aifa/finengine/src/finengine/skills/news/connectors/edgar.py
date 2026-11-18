"""SEC EDGAR per-company Atom feed connector.

Each US public company has a stable CIK (Central Index Key). EDGAR
exposes an Atom feed per (CIK, filing-type) at:

    https://www.sec.gov/cgi-bin/browse-edgar
        ?action=getcompany&CIK=<10-digit>&type=8-K&output=atom

8-K = material event report (dividends, M&A, CEO changes, profit
guidance). 10-Q = quarterly results. The connector pulls both into
one stream so the digest catches everything in real time, not when
CNBC happens to write about it.

User-Agent required by EDGAR ToS: include contact email.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)


# SEC explicitly throttles requests; their ToS asks for a contact UA.
_UA = "FA-CLAW IR Daily (compliance@fa-claw.dev)"

# ОГРАНИЧИТЕЛЬ ЧАСТОТЫ (19.08.2026). У нас 39 источников EDGAR, а сбор
# поднимает все источники одним `asyncio.gather` без потолка — значит к SEC
# уходит 39 одновременных запросов при их документированном пределе в 10/сек.
# Пауза приводит нас в соответствие с этим пределом.
#
# ⚠️ ЧЕСТНО О ПОЛЬЗЕ: я вводил её как ЛЕЧЕНИЕ пустых лент 8-K и ошибся.
# Замер до и после — залпом содержимое у 29 лент из 39, с паузой у 27 —
# то есть разницы нет, троттлинга под нашей нагрузкой SEC не применяет.
# Настоящей причиной пустоты оказалась форма запроса (см. `edgar_form` в
# config.py: иностранные эмитенты подают 6-K, а не 8-K).
#
# Пауза оставлена не ради этого, а потому что превышать опубликованный предел
# нельзя независимо от того, наказывают ли за это сегодня. Цена — около шести
# секунд на прогон в 270. Если понадобится ускорение, снимать её можно.
#
# Задержка глобальная на процесс: коннекторы синхронные и живут в разных
# потоках `asyncio.to_thread`, поэтому нужен именно потоковый замок.
_MIN_INTERVAL_S = 0.15          # предел SEC — 10 запросов в секунду
_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """Держит паузу между обращениями к SEC для всего процесса."""
    global _last_call
    with _rate_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


class EDGARConnector:
    """Pulls 8-K + 10-Q Atom feed for one company by CIK.

    The SourceConfig's `url` field encodes the CIK + filing-type query
    string. We could also accept multiple types per source but separate
    feeds keep auto-source-gen simple.
    """

    def __init__(self, source: SourceConfig):
        self.source = source

    def fetch(self, *, since_hours: int = 48, limit: int = 20) -> list[NewsItem]:
        try:
            import feedparser
            import httpx
        except ImportError:
            log.error("feedparser/httpx not installed — EDGAR skipped")
            return []

        _throttle()
        try:
            r = httpx.get(
                self.source.url,
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": _UA,
                    "Accept": "application/atom+xml, application/xml;q=0.9",
                },
            )
            if r.status_code != 200:
                log.warning("EDGAR %s: HTTP %d", self.source.id, r.status_code)
                return []
        except Exception as e:  # noqa: BLE001
            log.warning("EDGAR %s: fetch failed: %s", self.source.id, e)
            return []

        try:
            parsed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            log.warning("EDGAR %s: parse failed: %s", self.source.id, e)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        out: list[NewsItem] = []
        for entry in parsed.entries[:limit]:
            # EDGAR Atom uses `updated` (filing acceptance time) or
            # `published`. Both are tz-aware ISO strings.
            import time
            t = (getattr(entry, "updated_parsed", None)
                 or getattr(entry, "published_parsed", None))
            if not t:
                continue
            try:
                published = datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except (ValueError, OverflowError):
                continue
            if published < cutoff:
                continue

            title = _clean(getattr(entry, "title", "") or "")
            link = (getattr(entry, "link", "") or "").strip()
            if not title or not link:
                continue

            summary = _clean(getattr(entry, "summary", "") or
                             getattr(entry, "description", "") or "")
            # EDGAR titles look like "8-K - Apple Inc. (0000320193) (Filer)"
            # — extract filing type for snippet
            if not summary:
                summary = title  # at least include the filing type label

            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type=self.source.type,  # type: ignore[arg-type]
                region=self.source.region,  # type: ignore[arg-type]
                source_weight=self.source.weight,
                title=title[:200],
                snippet=summary[:600],
                url=link,
                published_at=published,
                raw_hash=canonical_hash(link, title),
            ))

        log.info("EDGAR %s: fetched %d items (window %dh)",
                 self.source.id, len(out), since_hours)
        return out
