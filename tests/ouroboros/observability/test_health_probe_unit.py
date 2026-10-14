"""The three probes against a bare app: what each makes of the one object a root publishes."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import psycopg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
from starlette.testclient import TestClient

from ouroboros.observability.health import (
    DATABASE_UNAVAILABLE,
    IHealthProbes,
    LoopHeartbeat,
    WorkerLoopProbes,
    database_failure,
    liveness_probe,
    readiness_probe,
    startup_probe,
)
from ouroboros.storage.db_url import postgres_connect_kwargs
from ouroboros.storage.probe_database import PROBE_WAIT_SEC, ProbeDatabase
from tests.helpers.schema_harness import scoped_url, worker_schema

_DRIVER_MESSAGE = (
    'connection to server at "db.internal" (10.0.0.5), port 5432 failed: '
    'FATAL: password authentication failed for user "ouroboros"'
)


class _Answers(IHealthProbes):
    def __init__(self, *, startup: str = "", readiness: str = "", liveness: str = "") -> None:
        self.answers = {"startup": startup, "readiness": readiness, "liveness": liveness}

    def startup(self) -> str:
        return self.answers["startup"]

    async def readiness(self) -> str:
        return self.answers["readiness"]

    def liveness(self) -> str:
        return self.answers["liveness"]


class _Database:
    def __init__(self) -> None:
        self.reachable = True
        self.engine: AsyncEngine = create_async_engine(
            "postgresql+psycopg://", async_creator=self._connect, poolclass=NullPool
        )

    async def _connect(self, *_args: Any, **_kwargs: Any) -> psycopg.AsyncConnection:
        if not self.reachable:
            raise psycopg.OperationalError(_DRIVER_MESSAGE)
        return await psycopg.AsyncConnection.connect(**postgres_connect_kwargs(scoped_url(worker_schema())))


class _SilentDatabase:
    def __init__(self, *, answers_the_handshake: bool) -> None:
        self._answers_the_handshake = answers_the_handshake
        self.invalidated = False
        self._over = asyncio.Event()

    async def connect(self) -> _SilentDatabase:
        if not self._answers_the_handshake:
            await self._silence()
        return self

    async def execute(self, _statement: object) -> None:
        await self._silence()

    async def close(self) -> None:
        return None

    async def invalidate(self) -> None:
        self.invalidated = True
        self._over.set()

    async def answer(self) -> None:
        self._over.set()
        await asyncio.sleep(0.01)

    async def _silence(self) -> None:
        while not self._over.is_set():
            # As a driver mid-wait: only the connection closing ends this, never the cancel.
            with contextlib.suppress(asyncio.CancelledError):
                await self._over.wait()


def _app(probes: object) -> Starlette:
    app = Starlette(
        routes=[
            Route("/start", endpoint=startup_probe),
            Route("/ready", endpoint=readiness_probe),
            Route("/live", endpoint=liveness_probe),
        ]
    )
    app.state.health_probes = probes
    return app


def _worker_over(database: _Database) -> WorkerLoopProbes:
    probe = ProbeDatabase(database.engine, deadline_sec=PROBE_WAIT_SEC)
    return WorkerLoopProbes(LoopHeartbeat("claim", stall_after_sec=60.0), probe)


def test_each_probe_asks_its_own_question() -> None:
    probes = _Answers(readiness="starting")
    with TestClient(_app(probes)) as client:
        assert client.get("/start").status_code == HTTP_200_OK
        ready = client.get("/ready")
        assert ready.status_code == HTTP_503_SERVICE_UNAVAILABLE
        assert ready.text == "starting"
        assert client.get("/live").status_code == HTTP_200_OK

        probes.answers["liveness"] = "the supervisor loop has not begun a tick for 91s"
        live = client.get("/live")
        assert live.status_code == HTTP_503_SERVICE_UNAVAILABLE
        assert "91s" in live.text


def test_a_success_carries_no_body_and_a_failure_is_bounded() -> None:
    with TestClient(_app(_Answers(startup="x" * 2_000))) as client:
        assert client.get("/ready").text == ""
        bounded = client.get("/start").text
        assert len(bounded) == 500
        assert bounded.endswith(" [truncated]"), "a cut body must say that it was cut"


def test_a_database_this_process_cannot_reach_is_readiness_alone() -> None:
    database = _Database()
    with TestClient(_app(_worker_over(database))) as client:
        assert client.get("/ready").status_code == HTTP_200_OK

        database.reachable = False
        refused = client.get("/ready")
        assert refused.status_code == HTTP_503_SERVICE_UNAVAILABLE
        assert refused.text == DATABASE_UNAVAILABLE
        assert client.get("/live").status_code == HTTP_200_OK
        assert client.get("/start").status_code == HTTP_200_OK

        database.reachable = True
        assert client.get("/ready").status_code == HTTP_200_OK


def test_the_failure_repeats_nothing_the_driver_said() -> None:
    database = _Database()
    database.reachable = False
    with TestClient(_app(_worker_over(database))) as client:
        refused = client.get("/ready")

    assert refused.status_code == HTTP_503_SERVICE_UNAVAILABLE
    for named in ("db.internal", "10.0.0.5", "5432", "ouroboros", "password"):
        assert named not in refused.text


def test_a_reply_that_never_arrives_is_a_503_inside_the_budget() -> None:
    silent = _SilentDatabase(answers_the_handshake=True)

    failure, elapsed, once_answering = _probed(silent)

    assert failure == DATABASE_UNAVAILABLE
    assert elapsed < 1.0
    assert silent.invalidated, "the connection that never answered was left open for the next probe"
    assert once_answering == "", "a database that answers again is ready with no restart"


def test_a_connection_that_never_opens_is_a_503_inside_the_budget() -> None:
    failure, elapsed, once_answering = _probed(_SilentDatabase(answers_the_handshake=False))

    assert failure == DATABASE_UNAVAILABLE
    assert elapsed < 1.0
    assert once_answering == "", "a database that answers again is ready with no restart"


def test_a_probe_cancelled_from_outside_takes_its_connection_with_it() -> None:
    silent = _SilentDatabase(answers_the_handshake=True)

    async def cancelled_mid_answer() -> None:
        probe = asyncio.ensure_future(ProbeDatabase(silent, deadline_sec=30.0).dial())
        try:
            await asyncio.sleep(0.01)
            probe.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await probe
            await asyncio.sleep(0.01)
        finally:
            # A dial left open would hold the loop shut instead of failing the assertion below.
            await silent.answer()

    asyncio.run(cancelled_mid_answer())

    assert silent.invalidated, "a probe cancelled before its deadline left the connection open"


def _probed(silent: _SilentDatabase) -> tuple[str, float, str]:
    async def probe() -> tuple[str, float, str]:
        database = ProbeDatabase(silent, deadline_sec=0.05)
        answer = asyncio.ensure_future(database_failure(database))
        started = time.monotonic()
        # Capped, so a probe that lost its deadline fails the suite instead of hanging it.
        done, _ = await asyncio.wait({answer}, timeout=5.0)
        assert done, "the probe never answered"
        failure, elapsed = answer.result(), time.monotonic() - started
        await silent.answer()
        return failure, elapsed, await database_failure(database)

    return asyncio.run(probe())


def test_a_worker_loop_reports_only_through_its_heartbeat() -> None:
    heartbeat = LoopHeartbeat("claim", stall_after_sec=0.0)
    probe = ProbeDatabase(_Database().engine, deadline_sec=PROBE_WAIT_SEC)
    with TestClient(_app(WorkerLoopProbes(heartbeat, probe))) as client:
        assert client.get("/start").status_code == HTTP_200_OK
        assert client.get("/ready").status_code == HTTP_200_OK
        assert client.get("/live").status_code == HTTP_200_OK, "a loop that has not begun is not a hung loop"

        heartbeat.beat()
        time.sleep(0.01)
        live = client.get("/live")
        assert live.status_code == HTTP_503_SERVICE_UNAVAILABLE
        assert "claim loop" in live.text
        assert client.get("/ready").status_code == HTTP_200_OK, "a stall is liveness' to report"


def test_a_heartbeat_reports_nothing_until_it_is_stale() -> None:
    fresh = LoopHeartbeat("supervisor", stall_after_sec=60.0)
    assert fresh.failure() == ""
    fresh.beat()
    assert fresh.failure() == ""

    stale = LoopHeartbeat("supervisor", stall_after_sec=0.0)
    stale.beat()
    time.sleep(0.01)
    assert "tick" in stale.failure()
