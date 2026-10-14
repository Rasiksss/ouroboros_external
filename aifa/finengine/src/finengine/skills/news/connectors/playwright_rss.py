"""Playwright-backed RSS fetcher for Cloudflare-protected feeds.

Business Wire and GlobeNewswire serve their RSS through Cloudflare with
the JS bot-challenge enabled — a plain httpx GET gets 403 even with
browser headers. Routing through real Chromium clears the challenge
(it executes the JS) and lets us parse the XML normally.

We render the URL with Playwright, take the page's `body` text content
(which Chromium presents as plain XML for `application/rss+xml`
responses), then hand it to feedparser exactly like the normal RSS
connector does.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from finengine.config import settings
from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)


class PlaywrightRSSConnector:
    """Same NewsItem contract as RSSConnector, but the bytes come from
    a JS-rendered fetch instead of plain HTTP."""

    def __init__(self, source: SourceConfig):
        self.source = source

    def fetch(self, *, since_hours: int = 48, limit: int = 50) -> list[NewsItem]:
        try:
            import feedparser
        except ImportError:
            log.error("feedparser not installed — RSS sources skipped")
            return []

        from finengine.skills.news.connectors.playwright_base import render_page

        # CF-protected wires need a non-RU egress IP too, since the
        # JS challenge correlates fingerprint + ASN. Reuse the same
        # proxy the macro dashboard uses for EIA / Investing.
        proxy = settings.macro_foreign_proxy or None
        # RSS bodies are raw XML — Chromium never fires `networkidle`
        # on them (no images/scripts to wait for). Use DCL and skip
        # the post-wait; the CF challenge (if any) resolves on the
        # initial navigation anyway.
        html = render_page(
            self.source.url, timeout_s=30.0, proxy=proxy,
            wait_until="domcontentloaded", post_wait_ms=0,
        )
        if not html:
            log.warning("PlaywrightRSS %s: render failed", self.source.id)
            return []

        # Chromium wraps raw XML in a <pre> tag when it detects RSS.
        # Strip the wrappers so feedparser sees clean XML.
        body = _extract_xml(html)
        parsed = feedparser.parse(body)
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            log.warning("PlaywrightRSS %s: bozo=1, 0 entries",
                        self.source.id)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        out: list[NewsItem] = []
        for e in parsed.entries[:limit]:
            published_dt = _parse_dt(e) or datetime.now(timezone.utc)
            if published_dt < cutoff:
                continue
            title = getattr(e, "title", "") or ""
            url = getattr(e, "link", "") or ""
            snippet = (getattr(e, "summary", "")
                       or getattr(e, "description", "")
                       or title)
            if not title or not url:
                continue
            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type=self.source.type,  # type: ignore[arg-type]
                region=self.source.region,  # type: ignore[arg-type]
                source_weight=self.source.weight,
                title=title[:200],
                snippet=snippet[:600],
                url=url,
                published_at=published_dt,
                raw_hash=canonical_hash(url, title),
            ))
        log.info("PlaywrightRSS %s: %d items", self.source.id, len(out))
        return out


def _extract_xml(html: str) -> str:
    """Pull the raw RSS XML body out of Chromium's wrapper."""
    import re
    m = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.DOTALL)
    if m:
        # Chromium HTML-escapes the XML inside <pre>; unescape it.
        import html as _h
        return _h.unescape(m.group(1))
    return html


def _parse_dt(entry) -> "datetime | None":
    from datetime import datetime as _dt, timezone as _tz
    for fld in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, fld, None)
        if struct:
            try:
                return _dt(*struct[:6], tzinfo=_tz.utc)
            except (ValueError, TypeError):
                pass
    return None
