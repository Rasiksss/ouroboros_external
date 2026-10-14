"""The supervisor tier as its own process: what it composes, and what it refuses.

`server.py` is the full runtime — transport, supervisor and agent in one. This suite is
about the third composition root: that it hosts the supervisor loop and nothing that runs a
task, that the obligations a second root can forget are refusals rather than conventions,
and that the probe it serves answers about the loop this process actually holds.

The fixture gives the process a model provider before composing: `build_supervisor_app()`
runs the shared component start-up, which refuses a process with none, and the suite's
environment carries none.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import socket
import threading
import time

import pytest
from starlette.testclient import TestClient

from background_memory.infra.events import memory_event_publisher_factory
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.gateway.router import collect_routes
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.database import refused_runtime_database
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.supervisor_process import READY_TIMEOUT_SEC, wait_started

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROOT_PACKAGE = REPO_ROOT / "multiuser_config" / "supervisor"

_LOOP_UNDER_TEST = "supervisor-main-under-test"


@pytest.fixture
def model_provider(monkeypatch):
    """The credentials the shared start-up refuses a component process without."""
    from ouroboros.app.server_runtime import has_supervisor_provider
    from ouroboros.storage.settings.store import load_settings

    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "https://models.example/v1")
    assert has_supervisor_provider(load_settings())
    yield


@pytest.fixture
def supervisor_process(model_provider):
    """The supervisor tier, composed the way its own entrypoint composes it.

    The loop is started by the lifespan, so a test that never enters `TestClient` holds a
    composed process whose loop has not signalled anything yet — which is the state the
    readiness probe has to report as "not ready".
    """
    from multiuser_config.supervisor.app import build_supervisor_app

    reset_memory_event_publisher()
    process = build_supervisor_app()
    yield process
    process.stop_event.set()
    reset_runtime_for_tests()
    reset_memory_event_publisher()


def _paths(routes) -> set[tuple[str, tuple[str, ...]]]:
    return {(route.path, tuple(sorted(getattr(route, "methods", None) or ()))) for route in routes}


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class _Recorder(logging.Handler):
    """Records one logger's lines.

    Attached to the module's own logger rather than through `caplog`: the shared start-up
    configures logging for the process, which clears the root handlers pytest captures on.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_the_supervisor_root_serves_the_health_surface_only(supervisor_process) -> None:
    """The whole surface: the three probes and the core health route."""
    served = _paths(supervisor_process.app.router.routes)

    assert served == _paths(collect_routes(data_dir=REPO_ROOT, repo_dir=REPO_ROOT, browser_enabled=False))
    assert {path for path, _methods in served} == {
        "/start",
        "/ready",
        "/live",
        "/api/health",
    }


def test_the_supervisor_root_names_nothing_that_runs_a_task() -> None:
    """A tier that starts no run must not be able to reach for one either.

    `supervisor` is deliberately absent from the list: this root imports it by design,
    which is the inverse of the transport root's rule.
    """
    forbidden = (
        "ouroboros.agent_runtime",
        "ouroboros.agent",
        "new_core.tools",
        "ouroboros.mcp",
        "ouroboros.egress.a2a",
    )

    for path in ROOT_PACKAGE.rglob("*.py"):
        for module in _imported_modules(path):
            assert not any(module == name or module.startswith(f"{name}.") for name in forbidden), (
                f"{path.name} imports {module}"
            )


def test_readiness_is_not_ready_until_the_hosted_loop_signals(supervisor_process) -> None:
    """This tier's probe reports the loop it hosts, which is a condition it observes in itself."""
    client = TestClient(supervisor_process.app)

    # Composed but not started: the lifespan is what starts the loop.
    assert client.get("/ready").status_code == 503
    assert client.get("/start").status_code == 503
    assert client.get("/live").status_code == 200, "that window is the startup probe's"

    with client:
        wait_started(supervisor_process.app)
        assert client.get("/ready").status_code == 200
        assert client.get("/start").status_code == 200
        assert client.get("/live").status_code == 200


def test_a_loop_that_could_not_start_is_carried_on_the_probe(supervisor_process, monkeypatch) -> None:
    """ "Alive but not ticking" is the failure process liveness cannot see."""
    import supervisor.bootstrap as bootstrap

    def boom(**_kwargs):
        raise RuntimeError("wiring exploded")

    monkeypatch.setattr(bootstrap, "build_supervisor_runtime", boom)

    with TestClient(supervisor_process.app) as client:
        wait_started(supervisor_process.app)
        readiness = client.get("/ready")

        assert readiness.status_code == 503
        assert "wiring exploded" in readiness.text
        liveness = client.get("/live")
        assert liveness.status_code == 503, "a loop that could not start is a process to replace"
        assert "wiring exploded" in liveness.text
        assert client.get("/start").status_code == 200, "the start is over; the restart is liveness' to order"


def test_readiness_fails_on_an_unreachable_database(supervisor_process) -> None:
    with TestClient(supervisor_process.app) as client:
        wait_started(supervisor_process.app)
        with refused_runtime_database():
            assert client.get("/ready").status_code == 503


def test_a_loop_that_stops_ticking_fails_liveness_and_nothing_else(model_provider, monkeypatch) -> None:
    """The failure only the heartbeat sees: the thread is alive and no iteration begins."""
    import supervisor.runtime as runtime_module
    from multiuser_config.supervisor import app as root
    from ouroboros.observability.health import LoopHeartbeat

    released = threading.Event()
    handed: dict[str, LoopHeartbeat] = {}

    def hangs_after_one_beat(_settings, **kwargs) -> None:
        handed["heartbeat"] = kwargs["heartbeat"]
        kwargs["heartbeat"].beat()
        kwargs["ready_event"].set()
        released.wait(READY_TIMEOUT_SEC)

    monkeypatch.setattr(runtime_module, "run_supervisor", hangs_after_one_beat)
    monkeypatch.setattr(root, "LoopHeartbeat", lambda loop, **_: LoopHeartbeat(loop, stall_after_sec=0.2))
    reset_memory_event_publisher()
    process = root.build_supervisor_app()
    try:
        with TestClient(process.app) as client:
            wait_started(process.app)
            time.sleep(0.4)
            liveness = client.get("/live")

            assert liveness.status_code == 503
            assert "tick" in liveness.text
            assert client.get("/ready").status_code == 200, "a stall is not a readiness matter"

            # The probe reads the very object the loop was handed, not a copy.
            handed["heartbeat"].beat()
            assert client.get("/live").status_code == 200
    finally:
        released.set()
        process.stop_event.set()
        reset_runtime_for_tests()
        reset_memory_event_publisher()


def test_every_component_start_up_compares_the_deployment_keyspace(tmp_path, monkeypatch, model_provider) -> None:
    """The one refusal the shared start-up was missing, so all three components make it."""
    from multiuser_config.bootstrap import bootstrap_process
    from ouroboros.app.multiuser_validation import MultiuserConfigurationError, resolved_keyspace

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(repo_dir))
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "redis")
    monkeypatch.setenv("OUROBOROS_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("OUROBOROS_RUNTIME_DB_URL", "postgresql+psycopg://ouroboros@localhost:5432/ouroboros")
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", "ouroboros-shared")
    # A peer instance recorded this deployment's keyspace first, at another endpoint.
    monkeypatch.setenv("OUROBOROS_S3_ENDPOINT", "https://storage-a.example")
    SqlDeploymentKeyspaceRepository(runtime_persistence.current.engine).record_deployment_keyspace(resolved_keyspace())
    monkeypatch.setenv("OUROBOROS_S3_ENDPOINT", "https://storage-b.example")

    with pytest.raises(MultiuserConfigurationError) as refusal:
        bootstrap_process("supervisor")

    assert "storage-a.example" in str(refusal.value)
    assert "storage-b.example" in str(refusal.value)


def test_the_root_carries_no_refusal_of_its_own(tmp_path, monkeypatch, model_provider) -> None:
    """A tier that refused more than its peers is a fleet whose processes disagree."""
    from multiuser_config.supervisor.app import build_supervisor_app
    from ouroboros.app.multiuser_validation import MultiuserConfigurationError, validate_multiuser_configuration

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(repo_dir))
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("A2A_ENABLED", "true")

    with pytest.raises(MultiuserConfigurationError) as shared:
        validate_multiuser_configuration()
    with pytest.raises(MultiuserConfigurationError) as entrypoint:
        build_supervisor_app()

    assert str(entrypoint.value) == str(shared.value), "the same refusal the full runtime produces"


def test_the_tier_starts_the_supervisor_loop_and_no_runtime(supervisor_process) -> None:
    """The inverse of the transport root's assertion: this one *is* the supervisor tier."""
    with TestClient(supervisor_process.app):
        wait_started(supervisor_process.app)
        names = {thread.name for thread in threading.enumerate()}

    assert any(name.startswith("supervisor") for name in names), names
    assert not any("agent" in name for name in names), names


def test_the_supervisor_app_publishes_only_what_a_mounted_route_reads(supervisor_process) -> None:
    """With `browser_enabled=False` the one object the probes read is the whole set (`.rules/supervisor-process.md`)."""
    published = set(supervisor_process.app.state._state)

    assert published == {"health_probes"}
    for absent in ("drive_root", "repo_dir", "app_start", "message_gateway", "chat_session_id", "runtime_status"):
        assert absent not in published


def test_the_root_installs_the_publisher_of_the_trigger_this_tier_produces(supervisor_process) -> None:
    """The supervisor's chat writer announces every persisted row, so this tier is a producer.

    Installed by composition time, before a tick can persist anything: the live reader —
    `log_chat` — reads the holder at call time rather than snapshotting it, so what matters
    here is that a process which composed this root never holds the null publisher while its
    loop is running.
    """
    from background_memory.infra.events.inmemory import InMemoryEventPublisher

    del supervisor_process  # composed by the fixture; the holder is what is asserted

    assert isinstance(memory_event_publisher_factory.memory_publisher, InMemoryEventPublisher)


def test_the_root_installs_no_process_wide_readiness_holder(model_provider) -> None:
    """Its readers are the A2A server and the readiness gate, neither composed here.

    Reset *before* the composition rather than after it: a reset that ran afterwards would
    erase the very evidence this case is about, and a root that did call
    `configure_supervisor_readiness` would still show an empty holder.
    """
    from multiuser_config.supervisor.app import build_supervisor_app
    from ouroboros.app import supervisor_readiness

    supervisor_readiness.reset_supervisor_readiness_for_tests()
    reset_memory_event_publisher()
    process = build_supervisor_app()
    try:
        assert supervisor_readiness._ready_event is None, "composing must configure no holder"
        with TestClient(process.app):
            wait_started(process.app)
            assert supervisor_readiness._ready_event is None, "nor may the loop's own start"
    finally:
        process.stop_event.set()
        reset_runtime_for_tests()
        reset_memory_event_publisher()


def test_the_root_hands_the_loop_the_refusing_restart_hooks(model_provider, monkeypatch) -> None:
    """The hooks are the whole refusal, so what the root passes is the thing to assert.

    A root that handed `server.py`'s permissive pair instead would keep every other case
    here green while a live instance wrote restart markers to a local disk and re-execed.
    """
    import supervisor.runtime as runtime_module
    from multiuser_config.supervisor.app import build_supervisor_app

    captured: dict[str, object] = {}

    def record(_settings, **kwargs) -> None:
        captured.update(kwargs)
        kwargs["ready_event"].set()

    monkeypatch.setattr(runtime_module, "run_supervisor", record)
    reset_memory_event_publisher()
    process = build_supervisor_app()
    try:
        with TestClient(process.app):
            wait_started(process.app)
    finally:
        process.stop_event.set()
        reset_runtime_for_tests()
        reset_memory_event_publisher()

    refused, reason = captured["safe_restart"]()
    assert refused is False and "supervisor" in reason

    process.stop_event.clear()
    captured["request_restart"]()
    assert process.stop_event.is_set(), "the only restart available is this process stopping"
    assert "apply_settings_to_env" not in captured, "this tier spawns no subprocess"


def test_an_owners_restart_is_refused_before_it_touches_any_record(tmp_path) -> None:
    """A restart handled here would restart a process that runs no task.

    The refusal is the supplied hook rather than a check on the command path, so the handler
    returns at its first branch — before it re-execs or cancels the asker's running records,
    which on this tier are records another instance is executing.
    """
    from types import SimpleNamespace

    from multiuser_config.supervisor.app import safe_restart
    from supervisor.commands import handle_slash_command
    from tests.helpers.shared import TEST_SCOPE

    ok, reason = safe_restart()

    assert ok is False
    assert "supervisor" in reason

    sent: list[str] = []
    restarted: list[int] = []
    consulted: list[dict] = []
    ctx = SimpleNamespace(
        drive_root=tmp_path,
        DRIVE_ROOT=tmp_path,
        store=SimpleNamespace(list_tasks=lambda **kwargs: consulted.append(kwargs) or []),
        send_with_budget=lambda text, **_kwargs: sent.append(text),
        safe_restart=safe_restart,
        request_restart=lambda: restarted.append(1),
    )

    handle_slash_command("/restart", ctx=ctx, scope=TEST_SCOPE, source="browser", transport={})

    assert any("Restart cancelled" in text for text in sent)
    assert restarted == [], "nothing re-execs, so nothing may ask for one"
    assert consulted == [], "the refusal returns before any owner's records are read"


def test_the_process_names_the_tier_and_what_it_composed(model_provider) -> None:
    """Inferring a tier from "does this environment hold model credentials" names none of them."""
    from multiuser_config.supervisor import app as root

    recorder = _Recorder()
    logging.getLogger(root.__name__).addHandler(recorder)
    reset_memory_event_publisher()
    try:
        process = root.build_supervisor_app()
    finally:
        logging.getLogger(root.__name__).removeHandler(recorder)
    process.stop_event.set()
    reset_runtime_for_tests()
    reset_memory_event_publisher()

    composed = [line for line in recorder.lines if "Composed the supervisor tier" in line]
    assert composed, recorder.lines
    assert "InProcessMessageGateway" in composed[0]
    assert "InMemoryAgentEventConsumer" in composed[0]


def test_the_entrypoint_fails_when_its_port_is_taken(monkeypatch, tmp_path) -> None:
    """A real bind failure exits non-zero without drifting onto a neighbour's port."""
    from starlette.applications import Starlette

    from multiuser_config.supervisor import __main__ as entrypoint
    from multiuser_config.supervisor.app import SupervisorProcess

    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    monkeypatch.setattr(
        "multiuser_config.supervisor.app.build_supervisor_app",
        lambda: SupervisorProcess(app=Starlette(), stop_event=threading.Event(), get_error=lambda: ""),
    )
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", str(port))
    recorder = _Recorder()
    logging.getLogger("multiuser_config.bootstrap").addHandler(recorder)
    try:
        assert entrypoint.main() != 0
    finally:
        logging.getLogger("multiuser_config.bootstrap").removeHandler(recorder)
        taken.close()

    assert any(f"127.0.0.1:{port}" in line for line in recorder.lines), (
        "the refusal must name the address it could not take"
    )
    assert list(tmp_path.rglob("server_port")) == [], "a supervisor process shares no port file"


def test_a_refused_bind_stops_the_loop_it_had_already_started(monkeypatch) -> None:
    """uvicorn binds after the lifespan, so the loop starts before the port is known — and must stop with it."""
    import contextlib

    from starlette.applications import Starlette

    from multiuser_config.supervisor import __main__ as entrypoint
    from multiuser_config.supervisor.app import SupervisorProcess

    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    stop_event = threading.Event()
    loop_started = threading.Event()

    def tick_until_stopped() -> None:
        loop_started.set()
        while not stop_event.wait(0.01):
            pass

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        thread = threading.Thread(target=tick_until_stopped, name=_LOOP_UNDER_TEST, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=READY_TIMEOUT_SEC)

    monkeypatch.setattr(
        "multiuser_config.supervisor.app.build_supervisor_app",
        lambda: SupervisorProcess(app=Starlette(lifespan=lifespan), stop_event=stop_event, get_error=lambda: ""),
    )
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", str(port))

    try:
        assert entrypoint.main() != 0
    finally:
        stop_event.set()
        taken.close()

    assert loop_started.is_set(), "the lifespan no longer starts the loop, so this covers nothing"
    assert _LOOP_UNDER_TEST not in {thread.name for thread in threading.enumerate()}, (
        "a refused bind left the loop it had already started running"
    )


def test_the_join_outlives_the_longest_step_of_a_tick() -> None:
    """A join equal to one step of the tick expires on every signal that lands inside it.

    The duplicate check's model call is that step, so a 15 s join would abandon the thread
    mid-write on an ordinary deploy and still report a clean exit. The number is the
    supervisor's own, taken from it rather than restated here.
    """
    from multiuser_config.supervisor import app as root
    from supervisor.events import _DEDUP_DEADLINE_SEC
    from supervisor.runtime import TICK_STOP_BUDGET_SEC

    assert root._LOOP_JOIN_SEC == TICK_STOP_BUDGET_SEC
    assert root._LOOP_JOIN_SEC > _DEDUP_DEADLINE_SEC


def test_a_loop_that_will_not_stop_is_reported_rather_than_left_silent(model_provider, monkeypatch) -> None:
    """The process leaves a thread behind mid-write; that is not a clean stop."""
    import supervisor.runtime as runtime_module
    from multiuser_config.supervisor import app as root

    released = threading.Event()

    def never_stops(_settings, **kwargs) -> None:
        kwargs["ready_event"].set()
        released.wait(READY_TIMEOUT_SEC)

    monkeypatch.setattr(runtime_module, "run_supervisor", never_stops)
    monkeypatch.setattr(root, "_LOOP_JOIN_SEC", 0.05)
    reset_memory_event_publisher()
    process = root.build_supervisor_app()
    try:
        with TestClient(process.app):
            wait_started(process.app)

        assert "did not stop" in process.get_error(), "an abandoned loop must reach the exit code"
    finally:
        released.set()
        reset_runtime_for_tests()
        reset_memory_event_publisher()


def test_the_watcher_asks_for_an_exit_only_when_the_loop_died_first() -> None:
    """Every ordinary shutdown sets the same stop event, so the event alone says nothing.

    Uvicorn takes the signal — setting `should_exit` itself — and the lifespan sets the stop
    event on the way down. A watcher that concluded "the loop died" from the event would
    report a dead loop on every deploy and ask an already-exiting server to exit.
    """
    from multiuser_config.supervisor.__main__ import _stop_serving_when_the_loop_stops

    class _Server:
        def __init__(self, exiting: bool) -> None:
            self.should_exit = exiting

    from multiuser_config.supervisor import __main__ as entrypoint

    stop_event = threading.Event()
    stop_event.set()
    recorder = _Recorder()
    watcher_log = logging.getLogger(entrypoint.__name__)
    watcher_log.addHandler(recorder)
    # The claim is an INFO line and the suite's root logger sits above it.
    previous_level = watcher_log.level
    watcher_log.setLevel(logging.INFO)
    try:
        died_first = _Server(exiting=False)
        _stop_serving_when_the_loop_stops(died_first, stop_event, threading.Event())

        already_exiting = _Server(exiting=True)
        _stop_serving_when_the_loop_stops(already_exiting, stop_event, threading.Event())

        returned_first = _Server(exiting=False)
        served = threading.Event()
        served.set()
        _stop_serving_when_the_loop_stops(returned_first, stop_event, served)
    finally:
        watcher_log.removeHandler(recorder)
        watcher_log.setLevel(previous_level)

    assert died_first.should_exit is True
    assert returned_first.should_exit is False, "`serve()` returned first; nothing to ask"
    claims = [line for line in recorder.lines if "stopped on its own" in line]
    assert len(claims) == 1, "the claim belongs to the one case that is true of the loop"


@pytest.mark.parametrize(
    ("reported", "code"),
    [("", 0), ("Supervisor loop stopped: aborted after 3 consecutive tick failures", 1)],
)
def test_a_stopped_loop_stops_the_server_and_decides_the_exit_code(monkeypatch, reported: str, code: int) -> None:
    """The direction the lifespan cannot cover: the loop dying while the server is up.

    A process that kept serving a probe after its loop had stopped would answer for a tier it
    is no longer part of, and an orchestrator has to be able to tell a crashed loop from a
    deploy. Uvicorn is stubbed here and only here: what is under test is the watcher and the
    exit code, not uvicorn's own serving.
    """
    import uvicorn
    from starlette.applications import Starlette

    from multiuser_config.supervisor import __main__ as entrypoint
    from multiuser_config.supervisor.app import SupervisorProcess

    stop_event = threading.Event()
    process = SupervisorProcess(app=Starlette(routes=[]), stop_event=stop_event, get_error=lambda: reported)
    monkeypatch.setattr("multiuser_config.supervisor.app.build_supervisor_app", lambda: process)

    class _Server:
        """Serves until asked to exit, which is the watcher's job here."""

        def __init__(self, config: object) -> None:
            self.config = config
            self.should_exit = False
            # uvicorn sets this once serving and never unsets it; the exit code turns on it.
            self.started = False

        def run(self, sockets: list[socket.socket] | None = None) -> None:
            self.started = True
            # The loop stops on its own while the server is still up — `run_supervisor`'s
            # `finally` sets exactly this event on every path out.
            stop_event.set()
            deadline = time.monotonic() + READY_TIMEOUT_SEC
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.05)
            for bound in sockets or ():
                bound.close()

    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", "0")

    assert entrypoint.main() == code


def test_the_entrypoint_uses_no_port_sweep_or_port_file() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in ROOT_PACKAGE.rglob("*.py"))

    for forbidden in ("find_free_port", "write_port_file", "kill_process_on_port"):
        assert forbidden not in source
