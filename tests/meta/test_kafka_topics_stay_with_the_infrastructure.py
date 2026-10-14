from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (
    "background_memory",
    "config_registry",
    "infra",
    "multiuser_config",
    "new_core",
    "ouroboros",
    "supervisor",
    "transport",
)
ADMIN_CLIENT = "KafkaAdminClient"
ADMIN_OPERATIONS = frozenset(
    {
        "create_topics",
        "delete_topics",
        "describe_configs",
        "alter_configs",
        "create_partitions",
        "delete_records",
        "list_consumer_group_offsets",
    }
)


def _production_modules() -> list[tuple[str, ast.Module]]:
    paths = [REPO_ROOT / "server.py"]
    for root in PRODUCTION_ROOTS:
        paths.extend(path for path in sorted((REPO_ROOT / root).rglob("*.py")) if "__pycache__" not in path.parts)
    return [
        (str(path.relative_to(REPO_ROOT)), ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in paths
    ]


def _named(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Import | ast.ImportFrom):
        return [alias.name for alias in node.names]
    return []


def _imported_module(node: ast.AST) -> str:
    if isinstance(node, ast.ImportFrom):
        return node.module or ""
    if isinstance(node, ast.Import):
        return " ".join(alias.name for alias in node.names)
    return ""


def test_no_production_module_imports_the_kafka_admin_client() -> None:
    found = [
        f"{relative}:{node.lineno}"
        for relative, tree in _production_modules()
        for node in ast.walk(tree)
        if "kafka.admin" in _imported_module(node) or ADMIN_CLIENT in _named(node)
    ]

    assert found == [], f"production code reaches the Kafka admin client: {found}"


def test_no_production_module_names_a_topic_administration_operation() -> None:
    found = [
        f"{relative}:{node.lineno} {name}"
        for relative, tree in _production_modules()
        for node in ast.walk(tree)
        for name in _named(node)
        if name in ADMIN_OPERATIONS
    ]

    assert found == [], f"production code administers Kafka topics or group positions: {found}"


def test_the_client_factory_interface_offers_no_admin_client() -> None:
    from infra.kafka_client import IKafkaClientFactory

    assert not hasattr(IKafkaClientFactory, "admin")


def test_the_guard_parses_modules_under_every_production_root() -> None:
    parsed = {relative.split("/")[0] for relative, _tree in _production_modules()}

    assert [root for root in PRODUCTION_ROOTS if root not in parsed] == []
