"""Access to the synchronous engine composed by this process."""

from sqlalchemy.engine import Engine


def resolve_runtime_engine(capability: str) -> Engine:
    """Resolve the composed engine, identifying the caller if startup is incomplete."""
    # Repository composition imports adapters that resolve this engine at call time.
    from ouroboros.storage.runtime_persistence import runtime_persistence

    try:
        return runtime_persistence.current.engine
    except RuntimeError as exc:
        raise RuntimeError(f"runtime database is not composed for {capability}") from exc
