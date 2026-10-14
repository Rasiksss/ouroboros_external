"""Generic HTML-listing connector.

For sites that don't expose RSS but have a stable HTML structure where
news titles + URLs can be extracted via regex. Used for Минфин РФ
press-center and similar.

The connector reads `selectors` from the SourceConfig.url metadata:
the URL must be of form `<page_url>#link_pattern=<regex>;date_pattern=<regex>`.
Simpler approach: hardcode site-specific extractors keyed by `source.id`.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import httpx

from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _clean(s: str) -> str:
    s = _html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ───────────────────────── per-site extractors ─────────────────────────


def _minfin_ru(body: str, base_url: str) -> list[tuple[str, str, datetime | None]]:
    """Extract (title, full_url, published_at) tuples from Минфин РФ
    press-center HTML. Site format (2026-08): каждая новость — блок
    `<div class="news_card_min">` с датой `data-date="ДД.ММ.ГГ"` и
    заголовком в `<a class="news_title …">`.

    🔴 ДАТА ЗДЕСЬ ЕСТЬ, И ЭТО ВАЖНО. Прежняя редакция ставила всем
    записям `now()` с оговоркой «дату надёжно не достать» — и она была
    неверна: дата лежит в разметке карточки. Цена ошибки видна на живом
    выпуске 18.08 с окном сбора 8 часов: в «Прочие события дня» уехали
    четыре публикации Минфина возрастом 6–13 дней. Фильтр по окну в
    `fetch()` был на месте, но не срабатывал никогда, потому что все
    записи объявлялись свежими.

    Точность — до суток: часа публикации на странице списка нет.
    Поэтому сегодняшняя дата отдаётся как `now` (иначе при коротком
    окне вылетели бы сегодняшние же новости), а прошедшая — как полдень
    того дня по Москве.
    """
    items: list[tuple[str, str, datetime | None]] = []
    msk = timezone(timedelta(hours=3))
    today_msk = datetime.now(msk).date()
    for card in body.split('class="news_card_min"')[1:]:
        m_url = re.search(r'href="(/ru/press-center/\?id_4=\d+[^"]*)"', card)
        m_title = re.search(r'class="news_title[^"]*"[^>]*>([^<]{15,300})', card)
        if not m_url or not m_title:
            continue
        title = _clean(m_title.group(1))
        if not title or "контакты" in title.lower():
            continue
        published: datetime | None = None
        m_date = re.search(r'data-date="(\d{2})\.(\d{2})\.(\d{2})"', card)
        if m_date:
            d, mo, y = (int(x) for x in m_date.groups())
            card_date = date(2000 + y, mo, d)
            published = (
                datetime.now(timezone.utc) if card_date >= today_msk
                else datetime(card_date.year, card_date.month, card_date.day,
                              12, 0, tzinfo=msk).astimezone(timezone.utc)
            )
        items.append((title, f"https://minfin.gov.ru{m_url.group(1)}", published))
    return items


# Add more extractors here as needed; key by source.id
_EXTRACTORS: dict[str, Callable[[str, str], list[tuple[str, str, datetime | None]]]] = {
    "minfin_ru": _minfin_ru,
}


class HTMLConnector:
    """Site-specific HTML scraper. Picks the extractor by source.id."""

    def __init__(self, source: SourceConfig):
        self.source = source

    def fetch(self, *, since_hours: int = 48, limit: int = 30) -> list[NewsItem]:
        extractor = _EXTRACTORS.get(self.source.id)
        if extractor is None:
            log.warning("HTML connector: no extractor registered for %s",
                        self.source.id)
            return []

        try:
            r = httpx.get(
                self.source.url, timeout=30.0, follow_redirects=True,
                headers={"User-Agent": _UA, "Accept": "text/html"},
            )
            if r.status_code != 200:
                log.warning("HTML %s: HTTP %d", self.source.id, r.status_code)
                return []
        except Exception as e:  # noqa: BLE001
            log.warning("HTML %s: fetch failed: %s", self.source.id, e)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        out: list[NewsItem] = []

        try:
            triples = extractor(r.text, self.source.url)[:limit]
        except Exception as e:  # noqa: BLE001
            log.warning("HTML %s: extractor crashed: %s", self.source.id, e)
            return []

        for title, url, dt in triples:
            published = dt or datetime.now(timezone.utc)
            if published < cutoff:
                continue
            # Snippet defaults to title (we don't fetch detail pages from
            # the listing — would be 30+ extra requests per cycle).
            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type=self.source.type,  # type: ignore[arg-type]
                region=self.source.region,  # type: ignore[arg-type]
                source_weight=self.source.weight,
                title=title[:200],
                snippet=title[:600],
                url=url,
                published_at=published,
                raw_hash=canonical_hash(url, title),
            ))

        log.info("HTML %s: fetched %d items", self.source.id, len(out))
        return out
