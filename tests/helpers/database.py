from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from ouroboros.storage.probe_database import probe_database_factory


def dispose_probe_database() -> None:
    """Its own loop: a fixture may run before the test loop is installed, and asyncio.run would drop it."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(probe_database_factory.dispose())
    finally:
        loop.close()


def _refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))


@contextlib.contextmanager
def refused_connections(engine: Engine) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(engine, "connect", _refuse)
        yield


@contextlib.contextmanager
def refused_runtime_database() -> Iterator[None]:
    with refused_connections(probe_database_factory.database.engine.sync_engine):
        yield
