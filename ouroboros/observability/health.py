from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from starlette import status
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ouroboros.storage.probe_database import ProbeDatabase
from ouroboros.storage.runtime_backend import STORE_FAILURES

log = logging.getLogger(__name__)

STARTING = "starting"
DATABASE_UNAVAILABLE = "runtime database unavailable"
_BODY_LIMIT = 500
_TRUNCATED = " [truncated]"


class IHealthProbes(ABC):
    """One process's answers to its orchestrator: "" is success, anything else is the 503 body."""

    @abstractmethod
    def startup(self) -> str: ...

    @abstractmethod
    async def readiness(self) -> str: ...

    @abstractmethod
    def liveness(self) -> str: ...


class LoopHeartbeat:
    """When a loop last began an iteration; not the task heartbeat, which is a lease on a record.

    One float replaced whole, so the loop's thread and the probe's need no lock.
    """

    def __init__(self, loop: str, *, stall_after_sec: float) -> None:
        self._loop = loop
        self._stall_after_sec = stall_after_sec
        self._last_beat: float | None = None

    def beat(self, at: float | None = None) -> None:
        """Mark the loop alive as of now, or as of the moment ``at`` names.

        A loop that runs its work inline beats for itself. One that only starts tasks beats
        for the oldest of them, because a dispatcher that is alive is not the question a
        liveness probe asks — ``background_memory/runtime.py`` is the caller that passes one.
        """
        self._last_beat = time.monotonic() if at is None else at

    def failure(self) -> str:
        last = self._last_beat
        if last is None:
            return ""
        age = time.monotonic() - last
        if age <= self._stall_after_sec:
            return ""
        return f"the {self._loop} loop has not begun a tick for {age:.0f}s"


class ComposedProbes(IHealthProbes):
    def __init__(self, database: ProbeDatabase) -> None:
        self._database = database

    def startup(self) -> str:
        return ""

    async def readiness(self) -> str:
        return await database_failure(self._database)

    def liveness(self) -> str:
        return ""


class WorkerLoopProbes(IHealthProbes):
    def __init__(self, heartbeat: LoopHeartbeat, database: ProbeDatabase) -> None:
        self._heartbeat = heartbeat
        self._database = database

    def startup(self) -> str:
        return ""

    async def readiness(self) -> str:
        return await database_failure(self._database)

    def liveness(self) -> str:
        return self._heartbeat.failure()


async def database_failure(database: ProbeDatabase) -> str:
    try:
        await database.dial()
    except STORE_FAILURES as refusal:
        log.warning("The runtime database did not answer the readiness probe: %s", refusal)
        return DATABASE_UNAVAILABLE
    return ""


def probe_routes() -> list[Route]:
    return [
        Route("/start", endpoint=startup_probe),
        Route("/ready", endpoint=readiness_probe),
        Route("/live", endpoint=liveness_probe),
    ]


async def startup_probe(request: Request) -> Response:
    return _answer(_probes(request).startup())


async def readiness_probe(request: Request) -> Response:
    return _answer(await _probes(request).readiness())


async def liveness_probe(request: Request) -> Response:
    return _answer(_probes(request).liveness())


def _probes(request: Request) -> IHealthProbes:
    return request.app.state.health_probes


def _answer(failure: str) -> Response:
    if not failure:
        return Response(status_code=status.HTTP_200_OK)
    if len(failure) > _BODY_LIMIT:
        failure = failure[: _BODY_LIMIT - len(_TRUNCATED)] + _TRUNCATED
    return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=failure)
