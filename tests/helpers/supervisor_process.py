from __future__ import annotations

import time

from starlette.testclient import TestClient

READY_TIMEOUT_SEC = 20.0


def wait_started(app) -> None:
    client = TestClient(app)
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while client.get("/start").status_code != 200:
        assert time.monotonic() < deadline, "the loop did not signal that it started"
        time.sleep(0.05)


__all__ = ["READY_TIMEOUT_SEC", "wait_started"]
