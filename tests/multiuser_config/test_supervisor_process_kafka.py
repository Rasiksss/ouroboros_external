from __future__ import annotations

import threading

from starlette.testclient import TestClient

from supervisor.runtime_context import reset_runtime_for_tests
from tests.helpers.memory_event_conformance import reset_memory_event_publisher
from tests.helpers.supervisor_process import READY_TIMEOUT_SEC, wait_started


def test_a_kafka_supervisor_runs_only_its_loop_and_takes_no_broker_step_at_stop(monkeypatch, tmp_path) -> None:
    import supervisor.runtime as runtime_module
    from multiuser_config.bootstrap import ProcessBootstrap
    from multiuser_config.supervisor import app as root
    from multiuser_config.supervisor.dependencies import SupervisorDependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.inmemory import create_agent_event_transport
    from ouroboros.app.task_stop_notifications import create_task_stop_transport
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.kafka_composition import install_initialized_client_factory, record_client_builds, select_kafka
    from transport.infra.inmemory import InProcessInbox, InProcessMessageGateway, InProcessOutbox

    seen: list[str] = []
    select_kafka(monkeypatch)
    record_client_builds(monkeypatch, install_initialized_client_factory(monkeypatch), seen=seen)

    def _loop(_settings, **kwargs) -> None:
        kwargs["ready_event"].set()
        kwargs["stop_event"].wait(READY_TIMEOUT_SEC)
        seen.append("loop joined")

    _publisher, consumer = create_agent_event_transport()
    stop_publisher, _stop_consumer = create_task_stop_transport()
    monkeypatch.setattr(runtime_module, "run_supervisor", _loop)
    monkeypatch.setattr(
        root,
        "bootstrap_process",
        lambda _component: ProcessBootstrap(settings=load_settings(), drive_root=tmp_path, repo_dir=tmp_path),
    )
    monkeypatch.setattr(
        root,
        "build_dependencies",
        lambda **_kwargs: SupervisorDependencies(
            gateway=InProcessMessageGateway(inbox=InProcessInbox(), outbox=InProcessOutbox()),
            agent_events=consumer,
            work_signal=InProcessWorkSignal(),
            task_stop_publisher=stop_publisher,
        ),
    )
    reset_memory_event_publisher()
    process = root.build_supervisor_app()
    try:
        with TestClient(process.app):
            wait_started(process.app)
            beside_the_loop = [thread.name for thread in threading.enumerate() if "kafka" in thread.name.lower()]
    finally:
        process.stop_event.set()
        reset_runtime_for_tests()
        reset_memory_event_publisher()

    assert beside_the_loop == []
    assert seen == ["loop joined"]
    assert process.get_error() == ""
