# tests/conftest.py — shared pytest fixtures for the Ouroboros test suite.
#
# Loaded automatically by pytest before any test module runs.
# Cross-module helpers that are not pytest fixtures (e.g. SDK mock, extension
# runtime cleanup) live in ``tests/helpers/shared.py`` instead.
import asyncio
import pathlib

import pytest

pytest_plugins = ["pytester", "tests.helpers.postgres", "tests.helpers.revision_schema"]


def pytest_configure(config):
    from tests.helpers.blob_double import install_blob_double

    install_blob_double()


@pytest.fixture(scope="session", autouse=True)
def _worker_schema():
    from tests.helpers.schema_harness import install_worker_schema

    install_worker_schema("tests")


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


from tests.helpers.conftest_base import (  # noqa: F401 — re-exported for test imports
    _REAL_APP_ROOT,
    compose_runtime_store_for_settings,
    forget_composed_runtime_store,
    reject_real_data_dir as _reject_real_data_dir,
    seed_chat_entries,
)


def fail_reads_of(monkeypatch, filename: str, exc: Exception) -> None:
    """Make one file present-but-unreadable, by name."""
    from tests.helpers.conftest_base import fail_reads_of as _fail_reads_of

    _fail_reads_of(monkeypatch, filename, exc)


def refresh_test_config() -> None:
    """Rebuild config singleton from the current os.environ (call after monkeypatch)."""
    from tests.helpers.conftest_base import refresh_test_config as _refresh

    _refresh()


@pytest.fixture(autouse=True)
def _bootstrap_config_for_tests(_development_deploy_env, monkeypatch, tmp_path):
    """Rebuild config singleton from the current test process environment."""
    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    from tests.helpers.conftest_base import wrap_monkeypatch_env

    wrap_monkeypatch_env(monkeypatch)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-blob-double")
    refresh_test_config()
    from ouroboros.storage.blob import reset_storage_provider_for_tests
    from tests.helpers.user_settings import StubUserSettingsStore, reset_user_settings_for_tests

    reset_storage_provider_for_tests()
    reset_user_settings_for_tests(StubUserSettingsStore())
    yield
    from ouroboros.storage.settings.bootstrap import reset_config_for_tests

    reset_storage_provider_for_tests()
    reset_config_for_tests()


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
def restore_process_singletons():
    """Restore process-wide composition state after a test rewires it."""
    from new_core.coordination.registry import concurrency_budgets
    from new_core.tools.catalog import tool_catalog

    providers = list(tool_catalog.providers)
    yield
    concurrency_budgets.reset_for_tests()
    tool_catalog.initialize(providers)


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
    # Nothing is unset here: monkeypatch already undoes the three setenv calls when the test
    # ends. Unsetting them now would rebuild the config through the wrapped ``delenv``, and the
    # path resolver would fall back to its home-directory default — the real data dir.
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
def _reset_browser_identity():
    """Each test starts on the resolver an instance without the mode composes.

    The configured resolver now mints the owner's default agent through a registry, so the
    neutral installation carries the same fake registry the trust-on tests compose.
    """
    from tests.helpers.agent_registry import FakeAgentRegistry
    from transport.browser.identity import ConfiguredOwnerIdentity, browser_identity

    configured = ConfiguredOwnerIdentity(FakeAgentRegistry())
    browser_identity.initialize(configured)
    yield
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


@pytest.fixture(autouse=True)
def _reset_security_context():
    from tests.helpers.security_context import (
        install_fresh_security_context_collector,
        install_fresh_security_context_store,
    )

    install_fresh_security_context_store()
    install_fresh_security_context_collector()
    yield
    install_fresh_security_context_store()
    install_fresh_security_context_collector()


@pytest.fixture(autouse=True)
def _reset_async_engine():
    """Each test composes its own async runtime DB engine.

    The composition roots build it while they compose, so a test that composes a root
    leaves one behind; the next test's `initialize` would then refuse a holder that is
    already built, and every later composition in the session would fail with it.
    """
    from infra.async_engine import async_engine_factory

    yield
    asyncio.get_event_loop().run_until_complete(async_engine_factory.dispose())


@pytest.fixture(autouse=True)
def _reset_chat_encryption():
    from ouroboros.storage.message_field_crypto import reset_encryption_cache_for_tests

    reset_encryption_cache_for_tests()
    yield
    reset_encryption_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_storage_provider():
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests
    from tests.helpers.blob_double import reset_blob_double

    reset_storage_provider_for_tests()
    reset_blob_double()
    yield
    reset_storage_provider_for_tests()
    reset_blob_double()


#: Pre-v5.15 conftest exported four fixtures (``make_git_repo``, ``tool_context``,
#: ``make_chat_mock``, ``make_extension_skill``) that no test ever requested as a
#: parameter. They were removed in v5.15.0; tests build their own minimal repos /
#: contexts under ``tmp_path`` because the per-test layouts diverged enough that a
#: shared fixture was always wrong (different branch names, different ``ToolContext``
#: shapes, ``MagicMock`` vs real, etc.).


@pytest.fixture(autouse=True)
def _composed_runtime_store(_bootstrap_config_for_tests):
    """The tests' composition root: one store per test, over this worker's schema.

    Production composes the store in the lifespan and never migrates from inside it —
    several instances starting together would race each other through Alembic, and
    ``runtime_persistence.current`` raises rather than conjuring one on first use. The
    schema is created and migrated once per worker process, so a test composes only.

    Declared after the config bootstrap so the order is the one it reads like: the
    bootstrap composed a store of its own before this fixture ever ran.
    """
    from tests.helpers.schema_harness import reset_worker_schema

    forget_composed_runtime_store()
    compose_runtime_store_for_settings()
    yield
    forget_composed_runtime_store()
    reset_worker_schema()


@pytest.fixture
def runtime_db_url(blank_schema) -> str:
    from infra.migrations.runner import upgrade_head

    engine = blank_schema()
    upgrade_head(engine)
    return str(engine.url.render_as_string(hide_password=False))


@pytest.fixture
def engine_factory(runtime_db_url: str):
    from infra.async_engine import AsyncEngineFactory

    factory = AsyncEngineFactory()
    factory.initialize(runtime_db_url)
    yield factory
    asyncio.get_event_loop().run_until_complete(factory.dispose())


@pytest.fixture
def multiuser_mode(monkeypatch):
    """The instance declared multi-user, for what reads the mode at request time."""
    from ouroboros.storage.settings.store import reset_memory_settings_for_tests

    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    reset_memory_settings_for_tests()
    yield
    monkeypatch.delenv("OUROBOROS_MULTIUSER", raising=False)
    reset_memory_settings_for_tests()


@pytest.fixture(autouse=True)
def _drop_handlers_pinned_to_the_test_stream():
    from tests.helpers.log_handlers import dropping_handlers_pinned_to_the_test_stream

    with dropping_handlers_pinned_to_the_test_stream():
        yield
