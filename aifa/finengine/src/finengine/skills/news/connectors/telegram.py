"""Telegram public-channel reader via t.me/s/<channel> HTML preview.

This is Telegram's own server-side rendered preview endpoint — the same
one their iframe-preview uses when you share a channel link. It returns
the last ~20 messages as plain HTML, no auth required, no API key.

Why this approach over telethon:
- No phone number / SMS code required
- No my.telegram.org API ID/Hash (often blocked for new accounts)
- No persistent session file
- Just an HTTP GET on a public URL

Format spec (stable since 2019):
  <div class="tgme_widget_message" data-post="channel/N">
    <div class="tgme_widget_message_text"> ... message HTML ... </div>
    <a class="tgme_widget_message_date" href="https://t.me/channel/N">
      <time datetime="2026-05-28T15:30:42+00:00"></time>
    </a>
  </div>
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem, canonical_hash

log = logging.getLogger(__name__)


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")


def _clean_html(s: str) -> str:
    """HTML → plain text. <br> → newline (preserves paragraph breaks)."""
    if not s:
        return ""
    s = s.replace("<br/>", "\n").replace("<br>", "\n").replace("<br />", "\n")
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return s.strip()


def _first_line(text: str, *, max_chars: int = 200) -> str:
    """First non-empty line, capped — used as title for the digest."""
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line[:max_chars]
    return text[:max_chars].strip()


def _strip_links(text: str) -> str:
    return _URL_RE.sub("", text).strip()


class TelegramConnector:
    """Public-channel fetcher via t.me/s/<channel> HTML preview.
    No auth, no API key, no session file. One HTTP GET per fetch."""

    def __init__(self, source: SourceConfig):
        self.source = source
        # URL is like "https://t.me/cbrstocks" → channel slug = "cbrstocks"
        self.channel = source.url.rstrip("/").split("/")[-1].lstrip("@")

    def fetch(self, *, since_hours: int = 48, limit: int = 30) -> list[NewsItem]:
        try:
            import httpx
        except ImportError:
            log.error("httpx not installed — telegram sources skipped")
            return []
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.error("beautifulsoup4 not installed — telegram sources skipped")
            return []

        preview_url = f"https://t.me/s/{self.channel}"
        try:
            r = httpx.get(
                preview_url,
                timeout=20.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; FA-CLAW/1.0; IR digest)",
                    "Accept": "text/html",
                },
            )
            if r.status_code != 200:
                log.warning("telegram %s: HTTP %d", self.channel, r.status_code)
                return []
        except Exception as e:  # noqa: BLE001
            log.warning("telegram %s: fetch failed: %s", self.channel, e)
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        out: list[NewsItem] = []

        soup = BeautifulSoup(r.text, "html.parser")
        # iter through last messages — preview gives oldest-first; reverse
        # to process newest first, easier cutoff break.
        messages = soup.find_all("div", class_="tgme_widget_message")
        messages.reverse()

        for msg in messages[:limit]:
            # data-post: "cbrstocks/12345"
            data_post = msg.get("data-post", "")
            if "/" not in data_post:
                continue
            msg_id = data_post.split("/", 1)[1]

            # Timestamp
            time_el = msg.find("time", class_="time")
            if time_el is None:
                # newer markup variant
                date_el = msg.find("a", class_="tgme_widget_message_date")
                time_el = date_el.find("time") if date_el else None
            if time_el is None or not time_el.get("datetime"):
                continue
            try:
                msg_dt = datetime.fromisoformat(time_el["datetime"])
                if msg_dt.tzinfo is None:
                    msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                else:
                    msg_dt = msg_dt.astimezone(timezone.utc)
            except (ValueError, KeyError):
                continue
            if msg_dt < cutoff:
                continue

            # Body text — preserve line breaks
            text_el = msg.find("div", class_="tgme_widget_message_text")
            if text_el is None:
                # No text → forwarded media-only, skip
                continue
            raw_html = text_el.decode_contents()
            full_text = _clean_html(raw_html)
            if not full_text:
                continue

            title = _first_line(full_text, max_chars=200)
            snippet = _strip_links(full_text)[:600]
            url = f"https://t.me/{self.channel}/{msg_id}"

            out.append(NewsItem(
                source=self.source.display_name,
                source_id=self.source.id,
                source_type=self.source.type,  # type: ignore[arg-type]
                region=self.source.region,  # type: ignore[arg-type]
                source_weight=self.source.weight,
                title=title,
                snippet=snippet,
                url=url,
                published_at=msg_dt,
                raw_hash=canonical_hash(url, title),
            ))

        log.info("telegram %s: fetched %d items (window %dh)",
                 self.channel, len(out), since_hours)
        return out
