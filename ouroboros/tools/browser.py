"""Optional Playwright browser tools."""

from __future__ import annotations

import base64
import importlib
import logging
import re
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.settings.store import load_settings

log = logging.getLogger(__name__)

try:
    Stealth = importlib.import_module("playwright_stealth").__dict__["Stealth"]
    _HAS_STEALTH = True
except ImportError:
    Stealth = None
    _HAS_STEALTH = False

_MISSING_EXECUTABLE_RE = re.compile(r"Executable doesn't exist|playwright install", re.I)


def _browser_unavailable_message(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, ImportError) or "No module named" in text:
        return "⚠️ BROWSER_UNAVAILABLE: Playwright is not installed. Install Python dependencies from pyproject.toml (uv sync)."
    if _MISSING_EXECUTABLE_RE.search(text):
        return (
            "⚠️ BROWSER_UNAVAILABLE: Playwright Chromium is not installed. "
            "Run `python -m playwright install chromium` in the runtime environment."
        )
    return f"⚠️ BROWSER_INFRA_ERROR: {text}"


def _browser_error_message(exc: BaseException) -> str:
    text = str(exc)
    if text.startswith("⚠️ BROWSER_"):
        return text
    return f"⚠️ BROWSER_INFRA_ERROR: {text}"


def _browser_base_url() -> str:
    return str(load_settings().OUROBOROS_BROWSER_BASE_URL or "").strip()


def _normalize_browser_url(url: str) -> str:
    text = str(url or "").strip()
    if not text or not text.startswith("/") or text.startswith("//"):
        return text
    base_url = _browser_base_url()
    if not base_url:
        return text
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", text.lstrip("/"))


def _owner_thread_matches_current(browser_state: Any) -> bool:
    owner_thread = getattr(browser_state, "_thread", None)
    if owner_thread is not None:
        return owner_thread is threading.current_thread() and owner_thread.is_alive()
    stored_thread_id = getattr(browser_state, "_thread_id", None)
    return stored_thread_id is not None and stored_thread_id == threading.get_ident()


def _ensure_browser_executor(ctx: ToolContext) -> ThreadPoolExecutor:
    browser_state = ctx.browser_state
    executor = getattr(browser_state, "_executor", None)
    if executor is None:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ouroboros-browser")
        browser_state._executor = executor
    return executor


def _run_on_browser_thread(ctx: ToolContext, func: Callable[[], str]) -> str:
    browser_state = ctx.browser_state
    if getattr(browser_state, "_thread", None) is threading.current_thread():
        return func()
    executor = _ensure_browser_executor(ctx)
    return executor.submit(func).result()


def _ensure_browser(ctx: ToolContext) -> Any:
    """Create or reuse this context's browser; no module-level browser state."""
    browser_state = ctx.browser_state
    current_thread = threading.current_thread()
    current_thread_id = threading.get_ident()
    stored_thread_id = getattr(browser_state, "_thread_id", None)
    owner_thread = getattr(browser_state, "_thread", None)
    if (owner_thread is not None and not _owner_thread_matches_current(browser_state)) or (
        stored_thread_id is not None and stored_thread_id != current_thread_id
    ):
        log.debug("Discarding stale browser handles before creating a new browser")
        _forget_browser_handles(ctx)

    if browser_state.browser is not None:
        try:
            if (
                browser_state.browser.is_connected()
                and browser_state.page is not None
                and not browser_state.page.is_closed()
            ):
                return browser_state.page
        except Exception:
            log.debug("Browser connection check failed", exc_info=True)
        _cleanup_browser_handles(ctx)

    try:
        sync_playwright = importlib.import_module("playwright.sync_api").__dict__["sync_playwright"]
    except ImportError as exc:
        raise RuntimeError(_browser_unavailable_message(exc)) from exc

    if browser_state.pw_instance is None:
        browser_state.pw_instance = sync_playwright().start()
        browser_state.playwright = browser_state.pw_instance
        browser_state._thread_id = current_thread_id
        browser_state._thread = current_thread

    try:
        browser_state.browser = browser_state.pw_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=site-per-process",
                "--window-size=1920,1080",
            ],
        )
    except Exception as exc:
        cleanup_browser(ctx)
        raise RuntimeError(_browser_unavailable_message(exc)) from exc

    try:
        browser_state.page = browser_state.browser.new_page(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        if _HAS_STEALTH and Stealth is not None:
            Stealth().apply_stealth_sync(browser_state.page)
        browser_state.page.set_default_timeout(30000)
    except Exception as exc:
        cleanup_browser(ctx)
        raise RuntimeError(_browser_unavailable_message(exc)) from exc
    return browser_state.page


def _forget_browser_handles(ctx: ToolContext) -> None:
    browser_state = ctx.browser_state
    browser_state.page = None
    browser_state.browser = None
    browser_state.pw_instance = None
    browser_state.playwright = None
    browser_state._thread_id = None
    browser_state._thread = None


def _shutdown_browser_executor(ctx: ToolContext) -> None:
    browser_state = ctx.browser_state
    executor = getattr(browser_state, "_executor", None)
    browser_state._executor = None
    if executor is not None:
        executor.shutdown(wait=False)


def _cleanup_browser_handles(ctx: ToolContext) -> None:
    browser_state = ctx.browser_state
    for obj_name, method in (("page", "close"), ("browser", "close"), ("pw_instance", "stop")):
        obj = getattr(browser_state, obj_name, None)
        if obj is None:
            continue
        try:
            getattr(obj, method)()
        except Exception:
            log.debug("Failed to close browser resource %s", obj_name, exc_info=True)
    _forget_browser_handles(ctx)


def cleanup_browser(ctx: ToolContext) -> None:
    """Close browser resources attached to this context."""
    browser_state = ctx.browser_state
    stored_thread_id = getattr(browser_state, "_thread_id", None)
    current_thread = threading.current_thread()
    current_thread_id = threading.get_ident()
    owner_thread = getattr(browser_state, "_thread", None)
    executor = getattr(browser_state, "_executor", None)
    if owner_thread is not None and owner_thread is not current_thread:
        if executor is not None and owner_thread.is_alive():
            try:
                executor.submit(_cleanup_browser_handles, ctx).result(timeout=10)
            except Exception:
                log.debug("Failed to clean browser resources on owner thread", exc_info=True)
                _forget_browser_handles(ctx)
            finally:
                _shutdown_browser_executor(ctx)
            return
        log.debug(
            "Discarding browser resources created on another thread without Playwright cleanup: owner=%s current=%s",
            getattr(owner_thread, "ident", None),
            current_thread_id,
        )
        _forget_browser_handles(ctx)
        _shutdown_browser_executor(ctx)
        return
    if stored_thread_id is not None and stored_thread_id != current_thread_id:
        log.debug(
            "Discarding browser resources created on another thread without Playwright cleanup: owner=%s current=%s",
            stored_thread_id,
            current_thread_id,
        )
        _forget_browser_handles(ctx)
        _shutdown_browser_executor(ctx)
        return

    _cleanup_browser_handles(ctx)
    _shutdown_browser_executor(ctx)


def _apply_viewport(page: Any, viewport: str) -> None:
    if not viewport:
        return
    try:
        width_text, height_text = str(viewport).lower().split("x", 1)
        width = max(320, int(width_text))
        height = max(480, int(height_text))
    except (TypeError, ValueError):
        log.warning("Invalid browser viewport %r, expected WxH", viewport)
        return
    page.set_viewport_size({"width": width, "height": height})


_MARKDOWN_JS = """() => {
    const walk = (el) => {
        let out = '';
        for (const child of el.childNodes) {
            if (child.nodeType === 3) {
                const t = child.textContent.trim();
                if (t) out += t + ' ';
            } else if (child.nodeType === 1) {
                const tag = child.tagName;
                if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                    out += '\\n' + '#'.repeat(parseInt(tag[1])) + ' ';
                if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\\n';
                if (tag === 'LI') out += '\\n- ';
                if (tag === 'A') out += '[';
                out += walk(child);
                if (tag === 'A') out += '](' + (child.href||'') + ')';
            }
        }
        return out;
    };
    return walk(document.body);
}"""


def _remember_screenshot(page: Any, ctx: ToolContext) -> None:
    try:
        data = page.screenshot(type="png", full_page=False)
        ctx.browser_state.last_screenshot_b64 = base64.b64encode(data).decode("ascii")
    except Exception:
        log.debug("Failed to capture browser screenshot", exc_info=True)


def _extract_page_output(page: Any, output: str, ctx: ToolContext) -> str:
    if output == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode("ascii")
        ctx.browser_state.last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            "Call send_photo(image_base64='__last_screenshot__') to deliver it to the user."
        )
    if output == "html":
        html = str(page.content())
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    if output == "markdown":
        text = str(page.evaluate(_MARKDOWN_JS))
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    text = str(page.inner_text("body"))
    return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


def _browse_page(
    ctx: ToolContext,
    url: str,
    output: str = "text",
    wait_for: str = "",
    timeout: int = 30000,
    viewport: str = "",
    selector: str = "",
    wait_until: str = "",
) -> str:
    return _run_on_browser_thread(
        ctx,
        lambda: _browse_page_on_browser_thread(
            ctx,
            url=_normalize_browser_url(url),
            output=output,
            wait_for=wait_for or selector,
            timeout=timeout,
            viewport=viewport,
            wait_until=wait_until or "domcontentloaded",
        ),
    )


def _browse_page_on_browser_thread(
    ctx: ToolContext,
    url: str,
    output: str = "text",
    wait_for: str = "",
    timeout: int = 30000,
    viewport: str = "",
    wait_until: str = "domcontentloaded",
) -> str:
    try:
        page = _ensure_browser(ctx)
        _apply_viewport(page, viewport)
        page.goto(url, timeout=timeout, wait_until=wait_until or "domcontentloaded")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout)
        if output != "screenshot":
            _remember_screenshot(page, ctx)
        return _extract_page_output(page, output, ctx)
    except RuntimeError as exc:
        message = _browser_error_message(exc)
        if "BROWSER_INFRA_ERROR" in message:
            cleanup_browser(ctx)
        return message
    except Exception as exc:
        cleanup_browser(ctx)
        return _browser_error_message(exc)


def _browser_action(
    ctx: ToolContext,
    action: str,
    selector: str = "",
    value: str = "",
    text: str = "",
    url: str = "",
    timeout: int = 5000,
) -> str:
    return _run_on_browser_thread(
        ctx,
        lambda: _browser_action_on_browser_thread(
            ctx,
            action=action,
            selector=selector,
            value=value or text or url,
            timeout=timeout,
        ),
    )


def _browser_action_on_browser_thread(
    ctx: ToolContext,
    action: str,
    selector: str = "",
    value: str = "",
    timeout: int = 5000,
) -> str:
    try:
        page = _ensure_browser(ctx)
        action_name = str(action or "").strip().lower()
        if action_name == "click":
            if not selector:
                return "Error: selector required for click"
            page.click(selector, timeout=timeout)
            page.wait_for_timeout(500)
            return f"Clicked: {selector}"
        if action_name in {"fill", "type"}:
            if not selector:
                return f"Error: selector required for {action_name}"
            page.fill(selector, value, timeout=timeout)
            return f"Filled {selector} with: {value}"
        if action_name == "select":
            if not selector:
                return "Error: selector required for select"
            page.select_option(selector, value, timeout=timeout)
            return f"Selected {value} in {selector}"
        if action_name == "screenshot":
            return _extract_page_output(page, "screenshot", ctx)
        if action_name == "evaluate":
            if not value:
                return "Error: value (JS code) required for evaluate"
            result = page.evaluate(value)
            out = str(result)
            return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
        if action_name == "scroll":
            direction = value or "down"
            if direction == "down":
                page.evaluate("window.scrollBy(0, 600)")
            elif direction == "up":
                page.evaluate("window.scrollBy(0, -600)")
            elif direction == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                return "Unknown scroll direction: use up, down, top, or bottom"
            return f"Scrolled {direction}"
        if action_name == "goto":
            if not value:
                return "Error: value or url required for goto"
            page.goto(_normalize_browser_url(value), wait_until="domcontentloaded", timeout=30000)
            return "Navigated."
        if action_name == "text":
            return str(page.inner_text(selector or "body"))
        return f"Unknown action: {action}. Use: click, fill, select, screenshot, evaluate, scroll, goto, text"
    except RuntimeError as exc:
        message = _browser_error_message(exc)
        if "BROWSER_INFRA_ERROR" in message:
            cleanup_browser(ctx)
        return message
    except Exception as exc:
        cleanup_browser(ctx)
        return _browser_error_message(exc)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema=ToolSchema(
                name="browse_page",
                description="Open a URL in a headless browser. Returns page content as text, html, "
                "markdown, or screenshot. Browser state persists across calls within a task.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to open"},
                        "output": {
                            "type": "string",
                            "enum": ["text", "html", "markdown", "screenshot"],
                            "description": "Output format",
                        },
                        "wait_for": {"type": "string", "description": "CSS selector to wait for"},
                        "timeout": {"type": "integer", "description": "Page timeout in ms"},
                        "viewport": {"type": "string", "description": "Viewport as WxH, for example 375x812"},
                    },
                    "required": ["url"],
                },
            ),
            handler=_browse_page,
            timeout_sec=180,
        ),
        ToolEntry(
            name="browser_action",
            schema=ToolSchema(
                name="browser_action",
                description="Perform an action on the current browser page: click, fill, select, "
                "screenshot, evaluate, scroll, goto, or text.",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "click",
                                "fill",
                                "type",
                                "select",
                                "screenshot",
                                "evaluate",
                                "scroll",
                                "goto",
                                "text",
                            ],
                            "description": "Action to perform",
                        },
                        "selector": {"type": "string", "description": "CSS selector for click/fill/select/text"},
                        "value": {
                            "type": "string",
                            "description": "Fill/select value, JS code for evaluate, URL for goto, or scroll direction",
                        },
                        "text": {"type": "string", "description": "Legacy alias for value"},
                        "url": {"type": "string", "description": "Legacy alias for goto target"},
                        "timeout": {"type": "integer", "description": "Action timeout in ms"},
                    },
                    "required": ["action"],
                },
            ),
            handler=_browser_action,
            timeout_sec=180,
        ),
    ]


__all__ = ["_browse_page", "_browser_action", "cleanup_browser", "get_tools"]
