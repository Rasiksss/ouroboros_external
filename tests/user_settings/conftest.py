from __future__ import annotations

import pytest

from config_registry.user_settings.infra.repositories.repo import SqlUserSettingsRepo


@pytest.fixture
def repo(engine_factory) -> SqlUserSettingsRepo:
    return SqlUserSettingsRepo(engine_factory.engine)
