"""Shared Playwright helper for JS-rendered news sources.

Several sources we want — e-disclosure.ru (RU corporate disclosures),
Business Wire, GlobeNewswire — return either an empty HTML shell or a
Cloudflare challenge to plain HTTP requests. Real Chromium fetches them
fine. This module gives connectors a single helper:

    html = render_page(
        "https://e-disclosure.ru/portal/company.aspx?id=3043",
        wait_selector="table.full-block-table",
    )

Threading model: every news connector runs in `asyncio.to_thread`, so
each call lands in a different worker thread. Playwright's sync API
uses greenlets bound to the thread that started them and refuses cross-
thread use ("Cannot switch to a different thread"). We therefore spin
up a fresh Chromium per call rather than caching a module-level browser.
Cost is ~1 s of browser start per fetch on top of the actual render;
for the ~20 JS-rendered sources we have this adds <30 s per pipeline
cycle, which runs once a day.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

# Cap concurrent Chromium instances. Each eats ~150-300 MB and the pipeline can
# fire 20+ JS-rendered fetches at once via asyncio.to_thread, which (a) OOMs the
# memory-capped backend (2 GB, no swap) and (b) above ~2-3 trips ServicePipe's
# bot rate-limit on the e-disclosure side. 3 balances IR-Calendar full-scan speed
# against those limits; don't raise further without adding swap / a bigger cap.
_RENDER_SEM = threading.BoundedSemaphore(3)


def render_page(
    url: str,
    *,
    wait_selector: Optional[str] = None,
    timeout_s: float = 25.0,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    wait_until: str = "networkidle",
    post_wait_ms: int = 1500,
) -> Optional[str]:
    """Open `url` in a one-shot headless Chromium and return the final
    rendered HTML. See module docstring for the threading rationale.

    `wait_until` follows Playwright semantics — default `networkidle`
    lets JS-based bot challenges (ServicePipe on e-disclosure.ru,
    Cloudflare on the wires) finish before we grab the DOM.
    `post_wait_ms` adds a final settle pause for SPAs that hydrate
    after `networkidle`.

    Returns None on any failure. Caller handles None as "skip this
    source for this cycle".

    `proxy` accepts the same string format httpx does
    (`http://user:pass@host:port`). Used by Cloudflare-blocked wires.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "playwright not installed — JS-rendered sources will be skipped."
        )
        return None

    # Whole call happens inside one `with` block so the Playwright
    # context, browser, and greenlets all live and die in this thread.
    # The semaphore caps parallelism — Chromium is heavy and remote
    # WAFs (ServicePipe) start rate-limiting hard above 2 concurrent.
    with _RENDER_SEM:
        return _render_locked(
            url, wait_selector=wait_selector, timeout_s=timeout_s,
            proxy=proxy, user_agent=user_agent, wait_until=wait_until,
            post_wait_ms=post_wait_ms,
        )


def _render_locked(
    url: str, *, wait_selector, timeout_s, proxy, user_agent,
    wait_until, post_wait_ms,
) -> Optional[str]:
    """Actual render. Wrapped by `render_page` under the semaphore."""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context_kwargs: dict = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": user_agent or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                # Many issuer IR / news sites run flaky or stale TLS certs
                # (e.g. corp.lenta.com served an expired cert → ERR_CERT_DATE_INVALID).
                # We only READ public pages here, so accept bad certs rather than
                # dropping the source entirely.
                "ignore_https_errors": True,
            }
            if proxy:
                context_kwargs["proxy"] = _parse_proxy(proxy)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            try:
                page.goto(url, timeout=int(timeout_s * 1000),
                          wait_until=wait_until)
                try:
                    page.wait_for_function(
                        """() => !document.body.innerHTML.includes('servicepipe.ru/static/checkjs')
                                && !document.title.toLowerCase().includes('just a moment')
                                && document.body.innerText.length > 500""",
                        timeout=int(timeout_s * 1000),
                    )
                except Exception:  # noqa: BLE001
                    log.info(
                        "playwright: anti-bot wait timed out for %s — "
                        "page may be partial", url,
                    )
                if wait_selector:
                    try:
                        page.wait_for_selector(
                            wait_selector, timeout=int(timeout_s * 1000),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "playwright: selector %r not found at %s: %s",
                            wait_selector, url, e,
                        )
                if post_wait_ms > 0:
                    page.wait_for_timeout(post_wait_ms)
                return page.content()
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        log.warning("playwright: render failed for %s: %s", url, e)
        return None


def _parse_proxy(proxy_url: str) -> dict:
    """Convert `http://user:pass@host:port` → Playwright's proxy dict."""
    from urllib.parse import urlparse
    p = urlparse(proxy_url)
    server = (
        f"{p.scheme}://{p.hostname}:{p.port}"
        if p.port else f"{p.scheme}://{p.hostname}"
    )
    out: dict = {"server": server}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out
