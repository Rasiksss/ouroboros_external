# tests/conftest.py — shared pytest fixtures for the Ouroboros test suite.
#
# Loaded automatically by pytest before any test module runs.
# Cross-module helpers that are not pytest fixtures (e.g. SDK mock, extension
# runtime cleanup) live in ``tests/_shared.py`` instead.
import asyncio
import pathlib

import pytest

pytest_plugins = ["pytester"]


def _mock_pollution_files(root: pathlib.Path) -> set[pathlib.Path]:
    try:
        return {p for p in root.iterdir() if p.is_file() and "<MagicMock" in p.name}
    except OSError:
        return set()


def pytest_sessionstart(session):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    session.config._ouroboros_initial_mock_pollution = _mock_pollution_files(repo_root)


def pytest_sessionfinish(session, exitstatus):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    initial = getattr(session.config, "_ouroboros_initial_mock_pollution", set())
    leaked = sorted(_mock_pollution_files(repo_root) - initial)
    if leaked:
        paths = ", ".join(str(p.relative_to(repo_root)) for p in leaked[:5])
        raise pytest.Exit(
            f"Test pollution: mock-named files leaked into repo root: {paths}",
            returncode=1,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Install a fresh asyncio event loop for the test *call* phase.

    Problem: asyncio.run() closes the loop it creates, leaving no current
    loop for the next test's asyncio.get_event_loop() call (RuntimeError).

    This hook installs a fresh loop BEFORE the test body and closes it
    AFTER, preventing cross-test contamination.  The loop is set to None
    after the call phase; a companion pytest_runtest_teardown hook
    installs a temporary loop for fixture finalizers.
    """
    test_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(test_loop)
    yield  # test body runs here
    test_loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _development_deploy_env(monkeypatch):
    """Default unit tests to the development context-builder surface.

    Headless/production behavior is covered in ``tests_headless/`` and in
    tests that explicitly set ``OUROBOROS_ENV=production``.
    """
    monkeypatch.setenv("OUROBOROS_ENV", "development")


def fail_reads_of(monkeypatch, filename: str, exc: Exception) -> None:
    """Make one file present-but-unreadable, by name.

    Migration ``0006`` reads legacy blobs through readers defined inside the
    migration module, and Alembic executes that module as its own object — so a
    patch on the importable copy would not reach it. ``pathlib`` is the shared
    boundary the file reader actually crosses; every other path is delegated so
    the rest of the run is untouched.
    """
    original = pathlib.Path.read_text

    def _read_text(self, *args, **kwargs):
        if self.name == filename:
            raise exc
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", _read_text)


def refresh_test_config() -> None:
    """Rebuild config singleton from the current os.environ (call after monkeypatch)."""
    from ouroboros.app.config import _refresh_path_aliases
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    bootstrap_config(force=True)
    _refresh_path_aliases()


@pytest.fixture
def identity_self_edit_on(monkeypatch):
    """Turn identity self-edit on for one test.

    The switch is hardcoded off in production, so the only way into the write
    path it guards is to replace the function itself — both where it is defined
    and in the one module that imported it by name.
    """
    from ouroboros.agent import context as agent_context
    from ouroboros.storage.core_memory.memory.domain import models

    monkeypatch.setattr(models, "identity_self_edit_enabled", lambda: True)
    monkeypatch.setattr(agent_context, "identity_self_edit_enabled", lambda: True)
    yield


@pytest.fixture(autouse=True)
def _bootstrap_config_for_tests(_development_deploy_env, monkeypatch, tmp_path):
    """Rebuild config singleton from the current test process environment."""
    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    _wrap_monkeypatch_env(monkeypatch)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    refresh_test_config()
    from ouroboros.storage.blob import reset_storage_provider_for_tests
    from ouroboros.storage.settings.user_settings import reset_user_settings_for_tests
    from tests.helpers.user_settings import StubUserSettingsStore

    reset_storage_provider_for_tests()
    reset_user_settings_for_tests(StubUserSettingsStore())

    yield
    from ouroboros.storage.settings.bootstrap import reset_config_for_tests

    reset_storage_provider_for_tests()
    reset_config_for_tests()


def _wrap_monkeypatch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    real_setenv = monkeypatch.setenv
    real_delenv = monkeypatch.delenv

    def setenv(name: str, value: str, raising: bool = True) -> None:
        del raising
        real_setenv(name, value)
        refresh_test_config()

    def delenv(name: str, raising: bool = True) -> None:
        real_delenv(name, raising=raising)
        refresh_test_config()

    monkeypatch.setenv = setenv  # type: ignore[method-assign]
    monkeypatch.delenv = delenv  # type: ignore[method-assign]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Keep a valid asyncio event loop available during the teardown phase.

    Fixture finalizers run during teardown (LIFO order).  If they call
    asyncio.get_event_loop() after a test that used asyncio.run(), they
    would raise RuntimeError because pytest_runtest_call already cleared
    the loop.  This hook installs a temporary loop for teardown and
    closes it afterwards.
    """
    teardown_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(teardown_loop)
    yield  # fixture finalizers and teardown run here
    teardown_loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def background_loop():
    """A real event loop running on its own thread — the shape ``server.py`` hands to the MCP
    and A2A managers, so a cross-thread bridge is exercised the way production reaches it."""
    import threading

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def isolate_settings(tmp_path, monkeypatch):
    """A settings file and data dir of this test's own, with the config module rebound.

    Yields the reloaded ``ouroboros.app.config`` module, which is what a round-trip test
    calls: the module caches paths at import time, so patching the env is not enough.
    """
    import importlib

    settings_path = tmp_path / "settings.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_ENV", "development")

    import ouroboros.app.config as cfg
    from ouroboros.storage.settings.store import reset_memory_settings_for_tests

    reset_memory_settings_for_tests()
    importlib.reload(cfg)
    yield cfg
    monkeypatch.delenv("OUROBOROS_SETTINGS_PATH", raising=False)
    monkeypatch.delenv("OUROBOROS_DATA_DIR", raising=False)
    monkeypatch.delenv("OUROBOROS_ENV", raising=False)
    reset_memory_settings_for_tests()
    importlib.reload(cfg)


@pytest.fixture
def preserve_environ():
    """Restore ``os.environ`` wholesale afterwards.

    For the tests that call ``apply_settings_to_env`` for real: it writes every schema
    default into the process environment and deletes what the schema does not name, which
    ``monkeypatch`` cannot undo because it never saw the writes.
    """
    import os

    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_runtime_store():
    import ouroboros.storage.runtime_store as runtime_store_mod

    runtime_store_mod._STORE_REF = None
    yield
    runtime_store_mod._STORE_REF = None


@pytest.fixture(autouse=True)
def _reset_core_memory_caches():
    """Rebind the core-memory services to each test's own runtime DB.

    Both are ``lru_cache``d per process, so a service built against one test's
    tmp drive root would otherwise answer queries in the next test.
    ``cache_clear()`` is the rebinding hook the capabilities expose instead of a
    test-only reset in business code.

    The name deliberately differs from the per-module ``_reset_core_memory_services``
    fixtures: a module-level fixture of the same name would shadow this one.
    """
    from ouroboros.storage.core_memory.memory import get_memory
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    for cached in (get_memory, get_scratchpad):
        cached.cache_clear()
    yield
    for cached in (get_memory, get_scratchpad):
        cached.cache_clear()


@pytest.fixture(autouse=True)
def _reset_storage_provider():
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


def _actor_type_for_seed_entry(entry: dict) -> str:
    direction = str(entry.get("direction") or "in").lower()
    if direction in ("out", "outgoing"):
        return "agent"
    if direction == "system":
        return "system"
    return "owner"


def seed_chat_entries(drive_root: pathlib.Path, entries: list[dict]) -> list[str]:
    """Persist chat-like dict entries to the runtime store; return message ids."""
    from ouroboros.storage.chat_store import append_chat_message
    from ouroboros.storage.runtime_store import init_runtime_store

    init_runtime_store(drive_root)
    message_ids: list[str] = []
    for index, entry in enumerate(entries):
        message_id = str(entry.get("client_message_id") or f"seed-{index}")
        persisted = append_chat_message(
            drive_root,
            actor_type=_actor_type_for_seed_entry(entry),  # type: ignore[arg-type]
            chat_id=entry.get("chat_id", 1),
            user_id=entry.get("user_id", 1),
            text=str(entry.get("text") or ""),
            source=str(entry.get("source") or "dialogs"),
            client_message_id=message_id,
            ts=str(entry.get("ts") or "") or None,
            message_type=str(entry.get("type") or ""),
            extra_metadata={key: entry[key] for key in ("tool_calls", "rounds") if key in entry},
        )
        assert persisted is not None
        message_ids.append(persisted.watermark_id)
    return message_ids


# Pre-v5.15 conftest exported four fixtures (``make_git_repo``, ``tool_context``,
# ``make_chat_mock``, ``make_extension_skill``) that no test ever requested as a
# parameter. They were removed in v5.15.0; tests build their own minimal repos /
# contexts under ``tmp_path`` because the per-test layouts diverged enough that a
# shared fixture was always wrong (different branch names, different ``ToolContext``
# shapes, ``MagicMock`` vs real, etc.).
