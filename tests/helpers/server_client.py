"""Shared hermetic Starlette client setup for server-bound tests."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


@contextmanager
def patched_server_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    browser_enabled: bool | None = None,
    reset_browser_ws: bool = False,
    reset_runtime: bool = False,
    reset_storage: bool = False,
) -> Iterator[TestClient]:
    """Return a server client without starting providers or the supervisor."""
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    data_dir.mkdir(exist_ok=True)
    repo_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(repo_dir))
    if browser_enabled is not None:
        monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "true" if browser_enabled else "false")

    from tests.conftest import refresh_test_config

    refresh_test_config()
    if reset_storage:
        from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

        reset_storage_provider_for_tests()
    if reset_browser_ws:
        from ouroboros.gateway import browser_ws

        browser_ws.reset_browser_ws_state_for_tests()

    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    with ExitStack() as stack:
        stack.enter_context(patch.object(server, "_start_supervisor_if_needed", lambda *_a, **_k: None))
        stack.enter_context(patch.object(server, "_apply_settings_to_env", lambda *_a, **_k: None))
        stack.enter_context(
            patch.object(server, "apply_runtime_provider_defaults", lambda settings: (settings, False, []))
        )
        stack.enter_context(patch("ouroboros.app.server_runtime.has_supervisor_provider", lambda _settings: False))
        server._supervisor_ready.set()
        client = TestClient(server.app)
        try:
            yield client
        finally:
            client.close()
            if reset_browser_ws:
                from ouroboros.gateway import browser_ws

                browser_ws.reset_browser_ws_state_for_tests()
            if reset_runtime:
                from supervisor.runtime_context import reset_runtime_for_tests

                reset_runtime_for_tests()
            sys.modules.pop("server", None)
