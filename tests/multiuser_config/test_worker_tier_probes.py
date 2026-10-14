"""The address every tier serves on, and the probe server a worker tier runs beside its loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from starlette.applications import Starlette
from starlette.status import HTTP_200_OK, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE

from multiuser_config.bootstrap import ProcessBootstrap, serve_probes, tier_config
from ouroboros.observability.health import LoopHeartbeat, WorkerLoopProbes
from ouroboros.storage.probe_database import create_probe_database
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.schema_harness import RUNTIME_DB_URL_PREFIX
from tests.helpers.shared import make_empty_extension_runtime, make_empty_skill_snapshot, make_skill_availability

BOOTSTRAP_LOGGER = "multiuser_config.bootstrap"


def _tier_settings() -> AppSettings:
    """A tier reaches the worker's database: its probe is composed from these settings."""
    import os

    return AppSettings(**{f"{RUNTIME_DB_URL_PREFIX}_1": os.environ[f"{RUNTIME_DB_URL_PREFIX}_1"]})


@pytest.fixture
def probe_database():
    database = create_probe_database(runtime_persistence.current.engine.url.render_as_string(hide_password=False))
    yield database
    asyncio.get_event_loop().run_until_complete(database.dispose())


@contextlib.contextmanager
def _configured_address(monkeypatch, host: str, port: str):
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_SERVER_HOST", host)
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", port)
    refresh_test_config()
    try:
        yield
    finally:
        for name in ("OUROBOROS_SERVER_HOST", "OUROBOROS_SERVER_PORT"):
            monkeypatch.delenv(name, raising=False)
        refresh_test_config()


def _free_port() -> int:
    with socket.socket() as scout:
        scout.bind(("127.0.0.1", 0))
        return int(scout.getsockname()[1])


@pytest.fixture
def tier_port(monkeypatch):
    port = _free_port()
    with _configured_address(monkeypatch, "127.0.0.1", str(port)):
        yield port


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as refused:
        return int(refused.code)


def test_a_taken_port_is_refused_naming_the_address(monkeypatch, caplog, probe_database) -> None:
    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    heartbeat = LoopHeartbeat("background-memory", stall_after_sec=0.0)
    try:
        with _configured_address(monkeypatch, "127.0.0.1", str(port)):
            with caplog.at_level(logging.ERROR, logger=BOOTSTRAP_LOGGER):
                assert serve_probes(WorkerLoopProbes(heartbeat, probe_database), tier="background-memory") is None
    finally:
        taken.close()

    assert f"background-memory tier could not serve on 127.0.0.1:{port}" in caplog.text


def test_a_blank_host_is_refused_rather_than_bound_everywhere(monkeypatch, caplog) -> None:
    with _configured_address(monkeypatch, "   ", "0"):
        with caplog.at_level(logging.ERROR, logger=BOOTSTRAP_LOGGER):
            assert tier_config(Starlette(), tier="agent-runtime") is None

    assert "blank host" in caplog.text


def test_a_server_that_never_served_is_not_a_clean_exit(tier_port) -> None:
    import uvicorn
    from starlette.applications import Starlette

    from multiuser_config.bootstrap import serve_tier

    @contextlib.asynccontextmanager
    async def refusing_lifespan(_app):
        raise RuntimeError("this tier cannot compose what it serves")
        yield  # pragma: no cover - the raise above is the whole of this lifespan

    config = tier_config(Starlette(lifespan=refusing_lifespan), tier="transport")
    assert config is not None

    assert serve_tier(uvicorn.Server(config), tier="transport") != 0


def test_a_probe_server_that_will_not_start_is_stopped_rather_than_abandoned(
    monkeypatch, tier_port, probe_database
) -> None:
    from multiuser_config import bootstrap

    heartbeat = LoopHeartbeat("agent-runtime", stall_after_sec=0.0)
    release_startup = threading.Event()
    entered_startup = threading.Event()
    servers = []
    startup = bootstrap.uvicorn.Server.startup

    async def delayed_startup(server, sockets=None) -> None:
        servers.append((server, threading.current_thread()))
        entered_startup.set()
        assert release_startup.wait(timeout=5.0), "the test did not release probe startup"
        await startup(server, sockets=sockets)

    monkeypatch.setattr(bootstrap.uvicorn.Server, "startup", delayed_startup)
    monkeypatch.setattr(bootstrap, "_PROBE_SERVER_WAIT_SEC", 0.0)

    try:
        stop = bootstrap.serve_probes(WorkerLoopProbes(heartbeat, probe_database), tier="agent-runtime")
        assert entered_startup.wait(timeout=5.0), "the probe server did not reach startup"
        assert stop is None
        release_startup.set()
        for server, thread in servers:
            thread.join(timeout=5.0)
            assert server.started, "the test did not exercise startup after the timeout"
            assert not thread.is_alive(), "the tier left a probe server behind that can still answer for it"
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{tier_port}/live", timeout=1)
    finally:
        for server, _thread in servers:
            server.should_exit = True
        release_startup.set()
        for _server, thread in servers:
            thread.join(timeout=5.0)


def test_the_probes_answer_beside_the_loop_and_stop_with_it(tier_port, probe_database) -> None:
    base = f"http://127.0.0.1:{tier_port}"
    heartbeat = LoopHeartbeat("agent-runtime", stall_after_sec=0.0)

    stop = serve_probes(WorkerLoopProbes(heartbeat, probe_database), tier="agent-runtime")
    assert stop is not None
    try:
        for path in ("/start", "/ready", "/live"):
            assert _status(f"{base}{path}") == HTTP_200_OK, f"{path} did not answer for a composed worker"
        # The gateway's set also carries `/api/health`, whose static "ok" would report a
        # stalled loop as healthy; this tier serves the three probes and nothing else.
        assert _status(f"{base}/api/health") == HTTP_404_NOT_FOUND, "the worker tier serves a surface beyond its probes"
        assert "agent-runtime-probes" in {thread.name for thread in threading.enumerate()}

        heartbeat.beat()
        time.sleep(0.01)
        assert _status(f"{base}/live") == HTTP_503_SERVICE_UNAVAILABLE, (
            "a loop that stopped beginning iterations is a process to replace"
        )
        assert _status(f"{base}/ready") == HTTP_200_OK, "a stall is liveness' to report"
    finally:
        stop()

    assert "agent-runtime-probes" not in {thread.name for thread in threading.enumerate()}
    with pytest.raises(OSError):
        urllib.request.urlopen(f"{base}/live", timeout=1)


def test_the_agent_runtime_tier_serves_its_probes_while_its_loop_runs(tier_port, tmp_path, monkeypatch) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    answered: dict[str, int] = {}

    class _ProbingRuntime:
        runtime_id = "runtime-under-test"

        def execute(self) -> None:
            base = f"http://127.0.0.1:{tier_port}"
            for path in ("/start", "/ready", "/live"):
                answered[path] = _status(f"{base}{path}")

        def drain(self, *, deadline_sec: float) -> bool:
            del deadline_sec
            return True

    _stub_the_tier(monkeypatch, agent_runtime_run, tmp_path, _ProbingRuntime())

    assert agent_runtime_run.main() == 0

    assert answered == {"/start": HTTP_200_OK, "/ready": HTTP_200_OK, "/live": HTTP_200_OK}
    assert "agent-runtime-probes" not in {thread.name for thread in threading.enumerate()}


def test_a_tier_that_cannot_serve_its_probes_runs_no_loop(tmp_path, monkeypatch) -> None:
    from multiuser_config.agent_runtime import run as agent_runtime_run

    executed: list[str] = []
    monitoring: list[str] = []

    class _RefusedRuntime:
        runtime_id = "runtime-under-test"
        drained = False

        def execute(self) -> None:
            executed.append("claimed")

        def drain(self, *, deadline_sec: float) -> bool:
            del deadline_sec
            self.drained = True
            return True

    runtime = _RefusedRuntime()
    _stub_the_tier(monkeypatch, agent_runtime_run, tmp_path, runtime)
    monkeypatch.setattr(agent_runtime_run, "serve_probes", lambda _probes, **_kwargs: None)
    monkeypatch.setattr(agent_runtime_run, "stop_process_monitoring", lambda: monitoring.append("stopped"))

    assert agent_runtime_run.main() == 1
    assert executed == [], "the tier claimed work nothing could probe"
    assert runtime.drained, "the refusal skipped the drain a served run goes through"
    assert monitoring == ["stopped"], "the refusal left this process's monitoring attached"


def test_the_memory_tier_takes_no_lease_when_it_cannot_serve_its_probes(tmp_path, monkeypatch) -> None:
    from multiuser_config.background_memory import run as background_memory_run

    executed: list[str] = []

    class _RefusedMemoryRuntime:
        def __init__(self, _consumer, *, handlers, stop_event, heartbeat) -> None:
            del handlers, stop_event, heartbeat

        def execute(self) -> None:
            executed.append("consumed")

    boot = ProcessBootstrap(
        settings=_tier_settings(), drive_root=pathlib.Path(tmp_path), repo_dir=pathlib.Path(tmp_path)
    )
    monkeypatch.setattr(background_memory_run, "bootstrap_process", lambda _component: boot)
    monkeypatch.setattr(background_memory_run, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(background_memory_run, "build_dependencies", lambda **_kwargs: _StubMemoryDependencies())
    monkeypatch.setattr("background_memory.runtime.MemoryRuntime", _RefusedMemoryRuntime)
    monkeypatch.setattr(background_memory_run, "serve_probes", lambda _probes, **_kwargs: None)

    assert background_memory_run.main() == 1
    assert executed == [], "the tier consumed under a lease nothing could probe"


class _StubMemoryDependencies:
    consumer = None
    handlers: dict = {}

    @staticmethod
    async def release() -> None:
        return None


def _stub_the_tier(monkeypatch, module, tmp_path, runtime) -> None:
    boot = ProcessBootstrap(
        settings=_tier_settings(), drive_root=pathlib.Path(tmp_path), repo_dir=pathlib.Path(tmp_path)
    )
    monkeypatch.setattr(module, "bootstrap_process", lambda _component: boot)
    composed = (
        (make_empty_skill_snapshot(), make_skill_availability(), make_empty_extension_runtime())
        if module.COMPONENT == "agent-runtime"
        else None
    )
    monkeypatch.setattr(module, "compose_process", lambda _boot, **_kwargs: composed)
    monkeypatch.setattr(module, "install_stop_signal", lambda _stop: None)
    monkeypatch.setattr(module, "build_dependencies", lambda **_kwargs: _StubDependencies())
    monkeypatch.setattr("ouroboros.agent_runtime.bootstrap.build_agent_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr(module, "_shutdown_tool_clients", lambda _loop, _files: None)


class _StubDependencies:
    events = None
    outbound = None
    work_signal = None
    task_stops = None
