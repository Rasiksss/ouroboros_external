"""Headless test defaults: file-backed storage unless overridden."""

from __future__ import annotations

import pytest

from ouroboros.storage.blob.factory import reset_storage_provider_for_tests


@pytest.fixture(autouse=True)
def _headless_file_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "file")
    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


@pytest.fixture(autouse=True)
def _bootstrap_config_for_headless(_headless_file_storage: None) -> None:
    from ouroboros.app.config import _refresh_path_aliases
    from ouroboros.storage.settings.bootstrap import bootstrap_config, reset_config_for_tests

    reset_config_for_tests()
    bootstrap_config(force=True)
    _refresh_path_aliases()
    yield
    reset_config_for_tests()
