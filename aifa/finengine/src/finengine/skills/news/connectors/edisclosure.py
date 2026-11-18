"""e-disclosure.ru connector — RU corporate material facts.

The portal at e-disclosure.ru is the official Russian channel where
public companies publish substantive events (dividend declarations,
board appointments, M&A, IFRS releases, etc.) by federal law. RBC and
Interfax rewrite these 1-3 hours after they're posted — being on
e-disclosure directly means we lead the news flow.

Per-company URL pattern:
    https://e-disclosure.ru/portal/company.aspx?id=<NNN>

The page is fully JS-rendered (an ASP.NET WebForms shell), so plain
HTTP gets a near-empty body. We use Playwright (chromium) to render
and then parse the disclosures table.

DOM shape (verified 2026-06):
    <table class="full-block-table">
      <tr>
        <td>27.05.2026 14:32</td>
        <td>Существенный факт</td>
        <td><a href="event.aspx?EventId=...">Решения совета директоров</a></td>
      </tr>
      ...
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)

# Map MSK timestamps in the disclosure rows back to UTC; the portal
# always renders Moscow local time, no timezone marker.
_MSK_OFFSET = timedelta(hours=3)


class EDisclosureConnector:
    """One per RU company (id = `edisclosure_<canonical-slug>`).

    `source.url` carries the portal company-page URL — the wire-up in
    config.py is responsible for building it from `companies.yaml`'s
    `edisclosure_id` field.
    """

    def __init__(self, source: SourceConfig):
        self.source = source
        # Display name carries the canonical entity ("Сбер (e-disclosure)").
        # Strip the connector suffix to derive the company name so we can
        # set matched_entities directly on each emitted NewsItem — the
        # default annotate() step keys on title/snippet, which for these
        # disclosures only contain the event type ("Завершение
        # размещения ценных бумаг") with no company string to match on.
        suffix = " (e-disclosure)"
        self._canonical = (
            source.display_name[:-len(suffix)]
            if source.display_name.endswith(suffix)
            else source.display_name
        )

    def fetch(self, *, since_hours: int = 48, limit: int = 30) -> list[NewsItem]:
        from finengine.skills.news.connectors.playwright_base import render_page

        # 45s timeout covers ServicePipe's JS-challenge solve
        # (typically 3-6s) plus the ASP.NET page render. No
        # `wait_selector` — `playwright_base` already waits for the
        # anti-bot marker to drop on its own.
        html = render_page(self.source.url, timeout_s=45.0)
        if not html:
            log.warning("e-disclosure %s: page render failed", self.source.id)
            return []

        rows = _parse_rows(html)
        if not rows:
            log.info("e-disclosure %s: 0 rows parsed (page may be paywalled)",
                     self.source.id)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        out: list[NewsItem] = []
        for ts_str, kind, title, href in rows[:limit]:
            dt = _parse_dt(ts_str)
            if dt is None or dt < cutoff:
                continue
            full_url = _absolutize(href, self.source.url)
            # Combine kind + title so the LLM and the renderer have the
            # disclosure category as part of the headline.
            full_title = f"[{kind}] {title}" if kind else title
            snippet = full_title  # the portal doesn't expose a preview
            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type="official",  # by law these are first-party
                region=self.source.region,
                source_weight=self.source.weight,
                title=full_title[:200],
                snippet=snippet[:600],
                url=full_url,
                published_at=dt,
                raw_hash=canonical_hash(full_url, full_title),
                # Pre-set the company match so ranker's topic_priority
                # boost applies — disclosure titles don't include the
                # company name themselves so annotate() would miss them.
                matched_entities=[self._canonical],
            ))
        log.info("e-disclosure %s: %d/%d rows in window",
                 self.source.id, len(out), len(rows))
        return out


# ─── HTML parsing helpers ───────────────────────────────────────────────


_ROW_RE = re.compile(
    # Disclosure row layout on company.aspx (verified 2026-06-07):
    #   <tr>
    #     <td>DD.MM.YYYY</td>                       ← date only
    #     <td class="datetime_nowrap">DD.MM.YYYY HH:MM</td>  ← full ts
    #     <td><a href="/portal/event.aspx?EventId=...">TITLE</a></td>
    #   </tr>
    # The TITLE is itself the disclosure category (e.g. "Завершение
    # размещения ценных бумаг"), so we don't need a separate kind cell.
    r"<tr[^>]*>\s*"
    r"<td[^>]*>\s*\d{2}\.\d{2}\.\d{4}\s*</td>\s*"
    r"<td[^>]*class=\"datetime_nowrap\"[^>]*>([^<]+)</td>\s*"
    r"<td[^>]*>\s*<a[^>]*href=\"([^\"]+)\"[^>]*>([^<]+)</a>",
    re.IGNORECASE | re.DOTALL,
)


def _parse_rows(html: str) -> list[tuple[str, str, str, str]]:
    """Return list of (date_str, kind, title, href). Tolerant — single
    pass; rows that don't match are silently dropped."""
    out: list[tuple[str, str, str, str]] = []
    for dt_str, href, title in _ROW_RE.findall(html):
        # kind is encoded in the title itself; pass empty so the
        # renderer doesn't add an extra `[kind]` prefix.
        out.append((_clean(dt_str), "", _clean(title), href.strip()))
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_dt(s: str) -> Optional[datetime]:
    """e-disclosure renders MSK timestamps. Convert to UTC."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s*(\d{2}):(\d{2})", s)
    if not m:
        # Date-only rows happen for the older entries; treat as start of day.
        m2 = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
        if not m2:
            return None
        d, mo, y = m2.groups()
        msk = datetime(int(y), int(mo), int(d), 0, 0)
    else:
        d, mo, y, hh, mm = m.groups()
        msk = datetime(int(y), int(mo), int(d), int(hh), int(mm))
    return (msk - _MSK_OFFSET).replace(tzinfo=timezone.utc)


def _absolutize(href: str, base: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)
