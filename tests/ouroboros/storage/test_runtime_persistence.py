import pytest

from ouroboros.storage.runtime_persistence import RuntimePersistenceProvider


def test_provider_owns_one_pool_until_close_and_recomposes_afterwards():
    provider = RuntimePersistenceProvider()
    with pytest.raises(RuntimeError, match="composition root"):
        _ = provider.current
    first = provider.initialize()
    original_pool = first.engine.pool
    assert provider.initialize() is first
    provider.close()
    assert first.engine.pool is not original_pool
    with pytest.raises(RuntimeError, match="composition root"):
        _ = provider.current
    provider.close()
    second = provider.initialize()
    try:
        assert second is not first
        assert second.engine is not first.engine
    finally:
        provider.close()
