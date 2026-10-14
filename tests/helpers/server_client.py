"""Shared hermetic Starlette client setup for server-bound tests."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryStore
from ouroboros.storage.chat_attachments import file_transaction
from ouroboros.storage.chat_store import InboundMessageStore
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.queue import get_task_queue
from supervisor.state import get_state_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository
from transport.browser.infra.repositories.owner_history.repo import SqlOwnerHistoryRepository


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

    from ouroboros.storage.probe_database import probe_database_factory
    from ouroboros.storage.settings.store import load_settings
    from tests.conftest import refresh_test_config
    from tests.helpers.database import dispose_probe_database

    refresh_test_config()
    # The lifespan composes this beside the store, and this client never enters one.
    dispose_probe_database()
    probe_database_factory.initialize_beside_store(load_settings())
    if reset_storage:
        from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

        reset_storage_provider_for_tests()
    if reset_browser_ws:
        from transport.browser import ws as browser_ws

        browser_ws.reset_browser_ws_state_for_tests()

    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server._starlette_app.state.owner_history_repository = SqlOwnerHistoryRepository(
        runtime_persistence.current.engine, history_transactions=file_transaction
    )
    audit = sql_audit_event_repository()
    state_repo = get_state_repository(
        data_dir,
        owner_state_repository=sql_owner_state_repository(),
        usage_repository=sql_usage_repository(),
        audit_event_repository=audit,
    )
    server._starlette_app.state.runtime_status = partial(
        server._runtime_status,
        task_queue=get_task_queue(data_dir, audit_event_repository=audit),
        state_repo=state_repo,
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(server, "_start_supervisor_if_needed", lambda *_a, **_k: None))
        stack.enter_context(patch.object(server, "_start_memory_runtime_if_needed", lambda *_a, **_k: None))
        stack.enter_context(patch.object(server, "_apply_settings_to_env", lambda *_a, **_k: None))
        stack.enter_context(
            patch.object(server, "apply_runtime_provider_defaults", lambda settings: (settings, False, []))
        )
        stack.enter_context(patch("ouroboros.app.server_runtime.has_supervisor_provider", lambda _settings: False))
        # One gateway per process, as in production: when a test bootstrapped a
        # supervisor runtime first, the app must publish and submit through the
        # same seam that runtime implements.
        from supervisor.runtime_context import try_get_runtime

        bootstrapped = try_get_runtime()
        if bootstrapped is not None:
            server._starlette_app.state.message_gateway = bootstrapped.gateway
        server._supervisor_ready.set()
        from infra.async_engine import async_engine_factory
        from tests.helpers.files import FileBlobDouble
        from transport.browser.ws import get_browser_ws_hub

        @asynccontextmanager
        async def file_lifespan(app):
            async_engine_factory.initialize_beside_store(load_settings())
            app.state.inbound_messages = InboundMessageStore(
                async_engine_factory.engine,
                SqlChatRegistryStore(),
                timeout=load_settings().OUROBOROS_FILE_IO_TIMEOUT_SEC,
            )
            app.state.file_blob = FileBlobDouble()
            hub = get_browser_ws_hub()
            hub.file_blob = app.state.file_blob
            try:
                yield
            finally:
                await hub.stop()
                await async_engine_factory.dispose()

        stack.enter_context(patch.object(server.app.router, "lifespan_context", file_lifespan))
        client = stack.enter_context(TestClient(server.app))
        try:
            yield client
        finally:
            client.close()
            if reset_browser_ws:
                from transport.browser import ws as browser_ws

                browser_ws.reset_browser_ws_state_for_tests()
            if reset_runtime:
                from supervisor.runtime_context import reset_runtime_for_tests

                reset_runtime_for_tests()
            sys.modules.pop("server", None)
