"""Isolate skill tests from the real data dir and settings store."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    """Point config, blob storage, and the runtime store at this test's tmp dir."""
    import ouroboros.storage.runtime_store as runtime_store_mod
    from ouroboros.app.config import _refresh_path_aliases
    from ouroboros.storage.blob import reset_storage_provider_for_tests
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    reset_config_for_tests()
    bootstrap_config(force=True)
    _refresh_path_aliases()
    reset_storage_provider_for_tests()
    runtime_store_mod._STORE_REF = None
    yield
    runtime_store_mod._STORE_REF = None
    reset_storage_provider_for_tests()
    reset_config_for_tests()
