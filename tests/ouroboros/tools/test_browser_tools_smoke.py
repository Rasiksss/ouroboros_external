from __future__ import annotations

import base64
import http.server
import socketserver
import threading

import pytest

from tests.helpers.shared import make_tool_ctx

pytestmark = pytest.mark.browser


class _StaticHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body><h1>Browser smoke OK</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture()
def static_page_url():
    try:
        server = socketserver.TCPServer(("127.0.0.1", 0), _StaticHandler)
    except PermissionError as exc:
        pytest.skip(f"local TCP bind is unavailable: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def test_browser_tools_launch_real_chromium(tmp_path, static_page_url):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")

    from ouroboros.tools.browser import _browse_page, _browser_action, cleanup_browser

    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    try:
        text = _browse_page(ctx, url=static_page_url)
        if text.startswith("⚠️ BROWSER_"):
            pytest.skip(text)
        assert "Browser smoke OK" in text

        screenshot = _browser_action(ctx, action="screenshot")
        if screenshot.startswith("⚠️ BROWSER_"):
            pytest.skip(screenshot)
        raw = base64.b64decode(ctx.browser_state.last_screenshot_b64 or "")
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        cleanup_browser(ctx)
