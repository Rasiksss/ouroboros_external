from __future__ import annotations

import pytest

from config_registry.agent_registry.infra.repositories.repo import SqlAgentRegistryRepo


@pytest.fixture
def registry(engine_factory) -> SqlAgentRegistryRepo:
    return SqlAgentRegistryRepo(engine_factory.engine)
