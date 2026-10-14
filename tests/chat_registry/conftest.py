from __future__ import annotations

import pytest

from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryRepo, SqlChatRegistryStore


@pytest.fixture
def store(tmp_path) -> SqlChatRegistryStore:
    """The synchronous face, over the runtime store this process composes."""
    from ouroboros.storage.runtime_persistence import runtime_persistence

    runtime_persistence.initialize()
    return SqlChatRegistryStore()


@pytest.fixture
def chats(store) -> SqlChatRegistryRepo:
    """The async face, over the same database — two faces, one table."""
    from infra.async_engine import AsyncEngineFactory
    from ouroboros.storage.runtime_engine import resolve_runtime_engine

    factory = AsyncEngineFactory()
    factory.initialize(str(resolve_runtime_engine("chat registry").url.render_as_string(hide_password=False)))
    return SqlChatRegistryRepo(factory.engine)
