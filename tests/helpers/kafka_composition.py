from __future__ import annotations

import ast
import pathlib

import pytest

from infra.kafka_client import KafkaClientFactory, KafkaConnection
from ouroboros.storage.settings.store import load_settings

KAFKA_TEST_TOPIC_PREFIX = "fleet-k"
KAFKA_TEST_MAX_MESSAGE_BYTES = 8_388_608
KAFKA_CLIENT_ACCESSORS = ("producer", "group_consumer", "partition_reader")
KAFKA_MODULES = (
    "infra.kafka_client",
    "transport.infra.kafka_gateway",
    "ouroboros.agent_events.kafka_transport",
    "background_memory.infra.events.kafka_transport",
)


def select_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "kafka")
    monkeypatch.setenv("OUROBOROS_KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9")
    monkeypatch.setenv("OUROBOROS_KAFKA_API_VERSION", "3.9")
    monkeypatch.setenv("OUROBOROS_KAFKA_TOPIC_PREFIX", KAFKA_TEST_TOPIC_PREFIX)
    monkeypatch.setenv("OUROBOROS_KAFKA_MAX_MESSAGE_BYTES", str(KAFKA_TEST_MAX_MESSAGE_BYTES))


def install_client_factory(monkeypatch: pytest.MonkeyPatch, factory: KafkaClientFactory) -> KafkaClientFactory:
    monkeypatch.setattr("infra.kafka_client.kafka_client_factory", factory)
    return factory


def install_initialized_client_factory(monkeypatch: pytest.MonkeyPatch) -> KafkaClientFactory:
    factory = KafkaClientFactory()
    factory.initialize(KafkaConnection.from_settings(load_settings()))
    return install_client_factory(monkeypatch, factory)


def record_client_builds(monkeypatch: pytest.MonkeyPatch, factory: KafkaClientFactory, *, seen: list[str]) -> None:
    for accessor in KAFKA_CLIENT_ACCESSORS:
        monkeypatch.setattr(factory, accessor, lambda *_, _name=accessor, **__: seen.append(f"{_name} built"))


def names_a_kafka_module(module: str) -> bool:
    return module == "kafka" or module.startswith("kafka.") or module in KAFKA_MODULES


def kafka_imports(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Import) and any(names_a_kafka_module(alias.name) for alias in child.names):
            found.append(child)
        elif isinstance(child, ast.ImportFrom) and child.level == 0 and names_a_kafka_module(child.module or ""):
            found.append(child)
    return found


def module_scope_kafka_imports(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    pending: list[ast.AST] = [ast.parse(path.read_text(encoding="utf-8"))]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.extend(ast.unparse(imported) for imported in kafka_imports(node))
        pending.extend(ast.iter_child_nodes(node))
    return found


__all__ = [
    "KAFKA_CLIENT_ACCESSORS",
    "KAFKA_MODULES",
    "KAFKA_TEST_MAX_MESSAGE_BYTES",
    "KAFKA_TEST_TOPIC_PREFIX",
    "install_client_factory",
    "install_initialized_client_factory",
    "kafka_imports",
    "module_scope_kafka_imports",
    "names_a_kafka_module",
    "record_client_builds",
    "select_kafka",
]
