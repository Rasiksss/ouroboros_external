from __future__ import annotations

import pathlib
import threading

import pytest

from background_memory.infra.events import memory_event_publisher_factory
from background_memory.interfaces import NullMemoryEventPublisher
from tests.helpers.memory_event_conformance import reset_memory_event_publisher


@pytest.fixture(autouse=True)
def _clean_memory_publisher():
    reset_memory_event_publisher()
    yield
    reset_memory_event_publisher()


@pytest.fixture
def db():
    from tests.helpers.background_memory_db import memory_database

    yield from memory_database()


@pytest.fixture
def kafka_selected(monkeypatch):
    from tests.helpers.kafka_composition import install_initialized_client_factory, select_kafka

    select_kafka(monkeypatch)
    return install_initialized_client_factory(monkeypatch)


def test_the_supervisor_tier_composes_the_kafka_broker_the_configuration_names(kafka_selected) -> None:
    from multiuser_config.supervisor.dependencies import build_dependencies
    from new_core.coordination.inprocess import InProcessWorkSignal
    from ouroboros.agent_events.kafka_transport import KafkaAgentEventConsumer
    from transport.infra.kafka_gateway import KafkaMessageGateway

    deps = build_dependencies(inbox_consumer="supervisor-1")

    assert isinstance(deps.gateway, KafkaMessageGateway)
    assert isinstance(deps.agent_events, KafkaAgentEventConsumer)
    assert isinstance(deps.work_signal, InProcessWorkSignal)


def test_the_runtime_composes_the_kafka_broker_the_configuration_names(tmp_path: pathlib.Path, kafka_selected) -> None:
    from multiuser_config.agent_runtime.dependencies import build_dependencies
    from ouroboros.agent_events.kafka_transport import KafkaAgentEventPublisher
    from supervisor.message_bus import LocalChatBridge
    from transport.infra.kafka_gateway import KafkaOutbox

    deps = build_dependencies(drive_root=tmp_path)

    assert isinstance(deps.events, KafkaAgentEventPublisher)
    assert isinstance(deps.outbound, LocalChatBridge)
    assert isinstance(deps.outbound._outbox, KafkaOutbox)


def test_the_memory_tier_composes_the_kafka_broker_the_configuration_names(
    tmp_path, kafka_selected, caplog, db
) -> None:
    from background_memory.infra.events.kafka_transport import KafkaMemoryEventConsumer
    from multiuser_config.background_memory.dependencies import build_dependencies

    logger = "multiuser_config.background_memory.dependencies"
    with caplog.at_level("INFO", logger=logger):
        deps = build_dependencies(
            drive_root=tmp_path,
            repo_dir=pathlib.Path.cwd(),
            stop_event=threading.Event(),
            engine=db.engine,
        )

    records = [record for record in caplog.records if record.name == logger]
    assert isinstance(deps.consumer, KafkaMemoryEventConsumer)
    assert isinstance(memory_event_publisher_factory.memory_publisher, NullMemoryEventPublisher)
    assert [record.levelname for record in records] == ["INFO"]
    assert "KafkaMemoryEventConsumer" in records[0].getMessage()


def test_no_root_imports_a_kafka_module_at_module_scope() -> None:
    from tests.helpers.kafka_composition import module_scope_kafka_imports

    repo = pathlib.Path(__file__).resolve().parents[2]
    roots = (
        "multiuser_config/bootstrap.py",
        "multiuser_config/transport/browser/app.py",
        "multiuser_config/supervisor/app.py",
        "multiuser_config/agent_runtime/run.py",
        "multiuser_config/background_memory/dependencies.py",
    )

    assert {root: module_scope_kafka_imports(repo / root) for root in roots} == {root: [] for root in roots}


def test_the_memory_tier_refuses_a_backend_its_log_has_no_arm_for(tmp_path, monkeypatch, caplog, db) -> None:
    from multiuser_config.background_memory import dependencies
    from ouroboros.app.broker_composition import UnhandledBrokerBackend

    monkeypatch.setattr(dependencies, "resolve_broker_backend", lambda: "carrier-pigeon")

    with (
        caplog.at_level("WARNING", logger="multiuser_config.background_memory.dependencies"),
        pytest.raises(UnhandledBrokerBackend) as refusal,
    ):
        dependencies.build_dependencies(
            drive_root=tmp_path,
            repo_dir=pathlib.Path.cwd(),
            stop_event=threading.Event(),
            engine=db.engine,
        )

    assert "OUROBOROS_BROKER_BACKEND" in str(refusal.value) and "'carrier-pigeon'" in str(refusal.value)
    assert not any("consume nothing" in record.getMessage() for record in caplog.records)
