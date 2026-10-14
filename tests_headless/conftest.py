"""Headless test defaults: the in-memory storage double and this worker's schema."""

from __future__ import annotations

import pytest

pytest_plugins = ["tests.helpers.revision_schema"]

from ouroboros.storage.blob.factory import reset_storage_provider_for_tests
from tests.helpers.blob_double import install_blob_double, reset_blob_double
from tests.helpers.database import dispose_probe_database
from tests.helpers.schema_harness import install_worker_schema, reset_worker_schema


def pytest_configure(config: pytest.Config) -> None:
    install_blob_double()


@pytest.fixture(scope="session", autouse=True)
def _worker_schema() -> None:
    install_worker_schema("headless")


@pytest.fixture(autouse=True)
def _reset_storage_provider() -> None:
    reset_storage_provider_for_tests()
    reset_blob_double()
    yield
    reset_storage_provider_for_tests()
    reset_blob_double()


@pytest.fixture(autouse=True)
def _bootstrap_config_for_headless(
    _reset_storage_provider: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from ouroboros.app.config import _refresh_path_aliases
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-blob-double")
    reset_config_for_tests()
    bootstrap_config(force=True)
    _refresh_path_aliases()
    yield
    reset_config_for_tests()


@pytest.fixture(autouse=True)
def _composed_runtime_store(_bootstrap_config_for_headless: None, tmp_path):
    """The tests' composition root: one store per test, over this worker's schema.

    The schema is created and migrated once per worker process, so nothing here runs
    Alembic; the reset that follows the teardown is what makes the next test
    indistinguishable from a fresh database.
    """
    from ouroboros.storage import runtime_persistence as runtime_store_module

    runtime_store_module.runtime_persistence.close()
    dispose_probe_database()
    runtime_store_module.runtime_persistence.initialize()
    yield
    runtime_store_module.runtime_persistence.close()
    dispose_probe_database()
    reset_worker_schema()


@pytest.fixture(autouse=True)
def _own_user_settings(_composed_runtime_store: None) -> None:
    from tests.helpers.user_settings import reset_user_settings_for_tests

    reset_user_settings_for_tests()
    yield
    reset_user_settings_for_tests()


@pytest.fixture(autouse=True)
def _drop_handlers_pinned_to_the_test_stream():
    from tests.helpers.log_handlers import dropping_handlers_pinned_to_the_test_stream

    with dropping_handlers_pinned_to_the_test_stream():
        yield
