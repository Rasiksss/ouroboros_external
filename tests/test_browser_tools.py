from __future__ import annotations

import sys
import threading
import types

from new_core.tools.context import ToolContext


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = types.SimpleNamespace(launch=lambda **_kwargs: browser)
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeBrowser:
    def __init__(self, page=None, *, new_page_error: Exception | None = None):
        self._page = page
        self._new_page_error = new_page_error
        self.closed = False

    def is_connected(self):
        return not self.closed

    def new_page(self, **_kwargs):
        if self._new_page_error is not None:
            raise self._new_page_error
        return self._page

    def close(self):
        self.closed = True


class _FakePage:
    def __init__(self, *, goto_error: Exception | None = None, click_error: Exception | None = None):
        self._goto_error = goto_error
        self._click_error = click_error
        self.closed = False
        self.goto_urls: list[str] = []
        self.owner_thread_id: int | None = None

    def is_closed(self):
        return self.closed

    def set_default_timeout(self, _timeout):
        return None

    def goto(self, *_args, **_kwargs):
        if self._goto_error is not None:
            raise self._goto_error
        self.owner_thread_id = threading.get_ident()
        if _args:
            self.goto_urls.append(str(_args[0]))

    def click(self, *_args, **_kwargs):
        if self._click_error is not None:
            raise self._click_error

    def screenshot(self, *_args, **_kwargs):
        if self.owner_thread_id is not None and self.owner_thread_id != threading.get_ident():
            raise RuntimeError("cannot switch to a different thread (which happens to have exited)")
        return b"\x89PNG\r\n\x1a\nfake"

    def inner_text(self, _selector):
        return "ok"

    def close(self):
        self.closed = True


def _install_fake_playwright(monkeypatch, pw):
    package = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    # ``_HAS_STEALTH`` is resolved once, when ouroboros.tools.browser is first
    # imported: it is True whenever the real playwright was importable then.
    # The fake page is not a playwright page, so pin the flag instead of
    # letting the suite's import order decide whether stealth runs.
    import ouroboros.tools.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_HAS_STEALTH", False)
    monkeypatch.setattr(browser_mod, "Stealth", None)


def test_browse_page_without_playwright_returns_stable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    from ouroboros.tools.browser import _browse_page

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    result = _browse_page(ctx, url="https://example.test")

    assert "BROWSER_UNAVAILABLE" in result
    assert "playwright" in result.lower()


def test_browser_action_without_playwright_returns_stable_error(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    from ouroboros.tools.browser import _browser_action

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    result = _browser_action(ctx, action="screenshot")

    assert "BROWSER_UNAVAILABLE" in result
    assert "playwright" in result.lower()


def test_browse_page_stores_last_screenshot(tmp_path, monkeypatch):
    page = _FakePage()
    browser = _FakeBrowser(page)
    pw = _FakePlaywright(browser)
    _install_fake_playwright(monkeypatch, pw)

    from ouroboros.tools.browser import _browse_page, cleanup_browser

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    try:
        result = _browse_page(ctx, url="https://example.test")

        assert result == "ok"
        assert ctx.browser_state.last_screenshot_b64
    finally:
        cleanup_browser(ctx)


def test_browse_page_cleans_up_after_partial_initialization_failure(tmp_path, monkeypatch):
    from ouroboros.tools.browser import _browse_page

    browser = _FakeBrowser(new_page_error=RuntimeError("Target closed"))
    pw = _FakePlaywright(browser)
    _install_fake_playwright(monkeypatch, pw)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    result = _browse_page(ctx, url="https://example.test")

    assert "BROWSER_INFRA_ERROR" in result
    assert browser.closed is True
    assert pw.stopped is True
    assert ctx.browser_state.browser is None
    assert ctx.browser_state.pw_instance is None
    assert ctx.browser_state.page is None
    assert ctx.browser_state._thread_id is None


def test_browse_page_cleans_up_after_page_infra_error(tmp_path):
    from ouroboros.tools.browser import _browse_page

    page = _FakePage(goto_error=RuntimeError("Target page, context or browser has been closed"))
    browser = _FakeBrowser(page)
    pw = _FakePlaywright(browser)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.browser_state.pw_instance = pw
    ctx.browser_state.browser = browser
    ctx.browser_state.page = page

    result = _browse_page(ctx, url="https://example.test")

    assert "BROWSER_INFRA_ERROR" in result
    assert page.closed is True
    assert browser.closed is True
    assert pw.stopped is True
    assert ctx.browser_state.browser is None
    assert ctx.browser_state.pw_instance is None
    assert ctx.browser_state.page is None


def test_browse_page_discards_browser_state_from_other_thread_without_cleanup(tmp_path, monkeypatch):
    from ouroboros.tools.browser import _browse_page

    stale_page = _FakePage()
    stale_browser = _FakeBrowser(stale_page)
    stale_pw = _FakePlaywright(stale_browser)
    fresh_page = _FakePage()
    fresh_browser = _FakeBrowser(fresh_page)
    fresh_pw = _FakePlaywright(fresh_browser)
    _install_fake_playwright(monkeypatch, fresh_pw)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.browser_state.pw_instance = stale_pw
    ctx.browser_state.browser = stale_browser
    ctx.browser_state.page = stale_page
    ctx.browser_state._thread_id = -1

    result = _browse_page(ctx, url="https://example.test")

    assert result == "ok"
    assert stale_page.closed is False
    assert stale_browser.closed is False
    assert stale_pw.stopped is False
    assert ctx.browser_state.page is fresh_page
    assert ctx.browser_state.browser is fresh_browser
    assert ctx.browser_state.pw_instance is fresh_pw
    assert ctx.browser_state._thread_id is not None


def test_browser_tools_keep_playwright_on_one_browser_thread(tmp_path, monkeypatch):
    from ouroboros.tools.browser import _browse_page, _browser_action, cleanup_browser

    page = _FakePage()
    browser = _FakeBrowser(page)
    pw = _FakePlaywright(browser)
    _install_fake_playwright(monkeypatch, pw)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    results: list[str] = []

    try:
        first = threading.Thread(target=lambda: results.append(_browse_page(ctx, url="https://example.test")))
        first.start()
        first.join(timeout=2)

        second = threading.Thread(target=lambda: results.append(_browser_action(ctx, action="screenshot")))
        second.start()
        second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert results[0] == "ok"
        assert results[1].startswith("Screenshot captured")
    finally:
        cleanup_browser(ctx)


def test_browse_page_discards_stale_browser_when_thread_id_was_reused(tmp_path, monkeypatch):
    from ouroboros.tools.browser import _browse_page

    class _DeadThread:
        def is_alive(self):
            return False

    stale_page = _FakePage(
        goto_error=RuntimeError("cannot switch to a different thread (which happens to have exited)")
    )
    stale_browser = _FakeBrowser(stale_page)
    stale_pw = _FakePlaywright(stale_browser)
    fresh_page = _FakePage()
    fresh_browser = _FakeBrowser(fresh_page)
    fresh_pw = _FakePlaywright(fresh_browser)
    _install_fake_playwright(monkeypatch, fresh_pw)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.browser_state.pw_instance = stale_pw
    ctx.browser_state.browser = stale_browser
    ctx.browser_state.page = stale_page
    ctx.browser_state._thread_id = threading.get_ident()
    ctx.browser_state._thread = _DeadThread()

    result = _browse_page(ctx, url="https://example.test")

    assert result == "ok"
    assert stale_page.closed is False
    assert stale_browser.closed is False
    assert stale_pw.stopped is False
    assert ctx.browser_state.page is fresh_page


def test_browse_page_resolves_relative_url_from_browser_base(tmp_path, monkeypatch):
    from ouroboros.tools.browser import _browse_page, cleanup_browser
    from tests.conftest import refresh_test_config

    page = _FakePage()
    browser = _FakeBrowser(page)
    pw = _FakePlaywright(browser)
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.setenv("OUROBOROS_BROWSER_BASE_URL", "http://127.0.0.1:8888")
    refresh_test_config()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    try:
        result = _browse_page(ctx, url="/subProjects/abc")

        assert result == "ok"
        assert page.goto_urls == ["http://127.0.0.1:8888/subProjects/abc"]
    finally:
        cleanup_browser(ctx)


def test_browser_action_cleans_up_after_page_infra_error(tmp_path):
    from ouroboros.tools.browser import _browser_action

    page = _FakePage(click_error=RuntimeError("Browser has been closed"))
    browser = _FakeBrowser(page)
    pw = _FakePlaywright(browser)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.browser_state.pw_instance = pw
    ctx.browser_state.browser = browser
    ctx.browser_state.page = page

    result = _browser_action(ctx, action="click", selector="#ok")

    assert "BROWSER_INFRA_ERROR" in result
    assert page.closed is True
    assert browser.closed is True
    assert pw.stopped is True
    assert ctx.browser_state.browser is None
    assert ctx.browser_state.pw_instance is None
    assert ctx.browser_state.page is None
