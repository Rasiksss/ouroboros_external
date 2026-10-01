from __future__ import annotations

import asyncio
import threading

import pytest

from ouroboros.app.supervisor_readiness import (
    configure_supervisor_readiness,
    reset_supervisor_readiness_for_tests,
    wait_for_supervisor_ready,
)


@pytest.fixture(autouse=True)
def _reset_readiness() -> None:
    reset_supervisor_readiness_for_tests()
    yield
    reset_supervisor_readiness_for_tests()


def test_wait_returns_when_ready_event_already_set() -> None:
    ready = threading.Event()
    ready.set()
    configure_supervisor_readiness(ready, lambda: None)
    asyncio.run(wait_for_supervisor_ready(timeout_sec=1.0, require_bridge=False))


def test_wait_times_out_when_supervisor_never_ready() -> None:
    configure_supervisor_readiness(threading.Event(), lambda: None)
    with pytest.raises(TimeoutError, match="Supervisor not ready"):
        asyncio.run(wait_for_supervisor_ready(timeout_sec=0.05, require_bridge=False))


def test_wait_raises_when_supervisor_reported_error() -> None:
    ready = threading.Event()
    ready.set()
    configure_supervisor_readiness(ready, lambda: "init blew up")
    with pytest.raises(RuntimeError, match="Supervisor failed: init blew up"):
        asyncio.run(wait_for_supervisor_ready(timeout_sec=1.0, require_bridge=False))
