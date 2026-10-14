from __future__ import annotations

import ast
import pathlib
import socket

import pytest
from starlette.testclient import TestClient

from ouroboros.gateway.router import collect_routes
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.database import refused_runtime_database

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROOT_PACKAGE = REPO_ROOT / "multiuser_config" / "config_registry"
OWNER_HEADER = "X-Ouroboros-Owner-Id"


@pytest.fixture
def registry_app():
    from multiuser_config.config_registry.app import build_registry_app

    return build_registry_app()


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_registry_root_names_neither_the_supervisor_nor_the_agent_runtime() -> None:
    forbidden = ("supervisor", "ouroboros.agent_runtime", "ouroboros.agent")
    for path in ROOT_PACKAGE.rglob("*.py"):
        for module in _imported_modules(path):
            assert not any(module == name or module.startswith(f"{name}.") for name in forbidden), (
                f"{path.name} imports {module}"
            )


def test_the_tier_starts_without_a_model_provider(registry_app) -> None:
    from ouroboros.app.server_runtime import has_supervisor_provider
    from ouroboros.storage.settings.store import load_settings

    assert not has_supervisor_provider(load_settings())
    assert registry_app is not None


def test_the_registry_does_not_announce_an_uncomposed_catalog(monkeypatch) -> None:
    from multiuser_config.config_registry.app import build_registry_app
    from ouroboros.app import agent_system_configuration_kafka as announcement

    monkeypatch.setattr("ouroboros.storage.settings.profile.is_server_profile", lambda: True)
    calls = []
    monkeypatch.setattr(announcement, "publish_agent_system_configuration_event", lambda **kwargs: calls.append(kwargs))
    build_registry_app()
    assert calls == []


def test_the_root_does_not_use_the_shared_component_start_up() -> None:
    tree = ast.parse((ROOT_PACKAGE / "app.py").read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "bootstrap_process" not in names
    assert "bootstrap_process" not in called
    assert {"validate_production_storage", "assert_schema_at_head", "validate_deployment_keyspace"} <= called
    assert "start_process_monitoring" in names
    assert "initialize_broker_backend" not in names | called
    assert "initialize_broker_backend" not in (ROOT_PACKAGE / "app.py").read_text(encoding="utf-8")


def test_the_registry_root_takes_no_broker_step(monkeypatch) -> None:
    from infra.kafka_client import KafkaClientFactory
    from multiuser_config.config_registry.app import build_registry_app
    from ouroboros.storage.settings.store import load_settings
    from tests.helpers.kafka_composition import install_client_factory

    monkeypatch.setenv("OUROBOROS_BROKER_BACKEND", "kafka")
    for key in [name for name in load_settings().model_dump() if name.startswith("OUROBOROS_KAFKA_")]:
        monkeypatch.delenv(key, raising=False)
    factory = install_client_factory(monkeypatch, KafkaClientFactory())

    with TestClient(build_registry_app()) as client:
        assert client.get("/ready").status_code == 200

    with pytest.raises(RuntimeError, match="not initialized"):
        _ = factory.connection


def test_the_tier_releases_the_synchronous_store_once_composed(registry_app) -> None:

    with pytest.raises(RuntimeError):
        _ = runtime_persistence.current


def test_the_tier_starts_no_supervisor_and_no_agent_runtime(registry_app) -> None:
    import threading

    with TestClient(registry_app):
        names = {thread.name for thread in threading.enumerate()}
    assert not any(name.startswith("supervisor") for name in names), names
    assert not any("agent-runtime" in name for name in names), names


def test_the_root_carries_no_refusal_of_its_own(tmp_path, monkeypatch) -> None:
    from multiuser_config.config_registry.app import build_registry_app
    from ouroboros.app.multiuser_validation import MultiuserConfigurationError, validate_multiuser_configuration

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(repo_dir))
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("A2A_ENABLED", "true")
    with pytest.raises(MultiuserConfigurationError) as shared:
        validate_multiuser_configuration()
    with pytest.raises(MultiuserConfigurationError) as entrypoint:
        build_registry_app()
    assert str(entrypoint.value) == str(shared.value), "the same refusal the full runtime produces"


def test_a_schema_below_the_builds_refuses_the_start(monkeypatch) -> None:
    from infra.migrations.runner import RuntimeSchemaOutdatedError
    from multiuser_config.config_registry.app import build_registry_app

    monkeypatch.setattr(
        "multiuser_config.config_registry.app.assert_schema_at_head",
        lambda _store: (_ for _ in ()).throw(RuntimeSchemaOutdatedError("schema is at version 3, build requires 12")),
    )
    with pytest.raises(RuntimeSchemaOutdatedError) as refusal:
        build_registry_app()
    assert "12" in str(refusal.value)


def test_the_tier_serves_the_registry_beside_the_health_surface(registry_app) -> None:
    from multiuser_config.config_registry.app import CHATS_MOUNT_PATH, MOUNT_PATH, SETTINGS_MOUNT_PATH

    health = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ())))
        for route in collect_routes(data_dir=REPO_ROOT, repo_dir=REPO_ROOT, browser_enabled=False)
    }
    served = {
        (route.path, tuple(sorted(getattr(route, "methods", None) or ())))
        for route in registry_app.router.routes
        if hasattr(route, "path")
    }
    assert health <= served
    with TestClient(registry_app) as client:
        document = client.get("/openapi.json").json()
    assert {(path, method) for (path, item) in document["paths"].items() for method in item} == {
        (f"{MOUNT_PATH}/", "get"),
        (f"{MOUNT_PATH}/", "post"),
        (f"{MOUNT_PATH}/{{agent_id}}", "delete"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}", "get"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/{{key}}", "put"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/{{key}}", "delete"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/mcp-servers", "get"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/mcp-servers/{{server_id}}/token", "put"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/mcp-servers/{{server_id}}/token", "delete"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/a2a-agents", "get"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/a2a-agents/{{remote_agent_id}}/token", "put"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/a2a-agents/{{remote_agent_id}}/token", "delete"),
        (f"{SETTINGS_MOUNT_PATH}/{{agent_id}}/skills", "get"),
        (f"{CHATS_MOUNT_PATH}/{{agent_id}}", "get"),
        (f"{CHATS_MOUNT_PATH}/{{agent_id}}", "patch"),
    }


def test_a_missing_owner_header_is_refused_in_the_mode(registry_app, multiuser_mode) -> None:
    """The mode is read per request: the app is composed first, then the mode declared."""
    from multiuser_config.config_registry.app import MOUNT_PATH

    with TestClient(registry_app) as client:
        refused = client.get(f"{MOUNT_PATH}/")
        served = client.get(f"{MOUNT_PATH}/", headers={OWNER_HEADER: "alice"})
    assert refused.status_code == 403
    assert served.status_code == 200
    assert served.json() == []


def test_the_probe_answers_for_this_process_and_names_no_owner(registry_app) -> None:
    with TestClient(registry_app) as client:
        readiness = client.get("/ready")
        liveness = client.get("/live")
    assert readiness.status_code == 200
    assert liveness.status_code == 200
    assert "owner" not in readiness.text.lower()


def test_readiness_fails_on_an_unreachable_database(registry_app) -> None:
    with TestClient(registry_app) as client, refused_runtime_database():
        assert client.get("/ready").status_code == 503


def test_the_tier_names_itself_at_start_up(capsys) -> None:
    from multiuser_config.config_registry.app import TIER, build_registry_app

    capsys.readouterr()
    build_registry_app()

    assert TIER in capsys.readouterr().out


def test_the_entrypoint_fails_when_its_port_is_taken(monkeypatch, tmp_path, caplog) -> None:
    """Bind or exit, the same way the transport root does: uvicorn binds inside its own serve."""
    import contextlib
    import logging

    from starlette.applications import Starlette

    from multiuser_config.config_registry import __main__ as entrypoint
    from tests.conftest import refresh_test_config

    taken = socket.socket()
    taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    lifespan_ran: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        lifespan_ran.append("started")
        try:
            yield
        finally:
            lifespan_ran.append("stopped")

    monkeypatch.setattr(
        "multiuser_config.config_registry.app.build_registry_app",
        lambda **_kwargs: Starlette(lifespan=lifespan),
    )
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", str(port))
    refresh_test_config()
    try:
        with caplog.at_level(logging.ERROR, logger="multiuser_config.bootstrap"):
            assert entrypoint.main() != 0
    finally:
        taken.close()
        for name in ("OUROBOROS_SERVER_HOST", "OUROBOROS_SERVER_PORT"):
            monkeypatch.delenv(name, raising=False)
        refresh_test_config()
    assert f"127.0.0.1:{port}" in caplog.text, "the refusal must name the address it could not take"
    assert list(tmp_path.rglob("server_port")) == [], "a registry process shares no port file"
    assert lifespan_ran == ["started", "stopped"], "a refused tier left its start-up half-done"


def test_the_entrypoint_uses_no_port_sweep_or_port_file() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in ROOT_PACKAGE.rglob("*.py"))
    for forbidden in ("find_free_port", "write_port_file", "kill_process_on_port"):
        assert forbidden not in source


def test_the_tier_serves_both_of_its_surfaces(registry_app) -> None:
    """Two mounts, each on its own path, and neither reachable through the other's."""
    from multiuser_config.config_registry.app import MOUNT_PATH, SETTINGS_MOUNT_PATH

    with TestClient(registry_app) as client:
        settings = client.get(f"{SETTINGS_MOUNT_PATH}/main", headers={OWNER_HEADER: "alice"})
        agents = client.get(f"{MOUNT_PATH}/", headers={OWNER_HEADER: "alice"})
    # Alice has no default agent yet — it is minted at her first ingress, not here.
    assert (settings.status_code, agents.status_code) == (404, 200)


def test_both_roots_serve_the_settings_surface_from_the_same_definitions(registry_app) -> None:
    """The routers are defined once; each root includes the same route functions."""
    from starlette.routing import Mount

    import server as single_process

    api = next(route.app for route in single_process.routes if isinstance(route, Mount) and route.path == "/api")
    by_server = {(path, method) for path, item in api.openapi()["paths"].items() for method in item}
    by_tier = {
        (path.removeprefix("/api"), method) for path, item in registry_app.openapi()["paths"].items() for method in item
    }
    assert by_tier, "the tier serves no owner-bearing route at all"
    assert by_server == by_tier


def test_the_settings_surface_names_no_transport() -> None:
    from config_registry.user_settings.api.http import endpoint

    source = pathlib.Path(endpoint.__file__).read_text(encoding="utf-8")
    assert "transport" not in source


def test_the_settings_mount_refuses_a_request_that_names_no_owner(registry_app, multiuser_mode) -> None:
    from multiuser_config.config_registry.app import SETTINGS_MOUNT_PATH

    with TestClient(registry_app) as client:
        refused = client.get(f"{SETTINGS_MOUNT_PATH}/main")
    assert refused.status_code == 403


def test_the_settings_surface_reaches_no_key_outside_the_declaration(registry_app) -> None:
    from config_registry.user_settings.api.http.endpoint import OWNER_SETTING_KEYS
    from multiuser_config.config_registry.app import MOUNT_PATH, SETTINGS_MOUNT_PATH

    with TestClient(registry_app) as client:
        agent_id = client.post(f"{MOUNT_PATH}/", json={"name": "sub"}, headers={OWNER_HEADER: "alice"}).json()[
            "agent_id"
        ]
        payload = client.get(f"{SETTINGS_MOUNT_PATH}/{agent_id}", headers={OWNER_HEADER: "alice"}).json()
    assert set(payload) == set(OWNER_SETTING_KEYS)


def test_the_registry_catalog_reads_selection_written_through_the_existing_settings_api(tmp_path, monkeypatch) -> None:
    from config_registry.user_settings.reader import user_settings_factory
    from multiuser_config.config_registry.app import build_registry_app

    package = tmp_path / "skills" / "example"
    (package / "agents").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example\ntype: instruction\n---\nInstructions\n"
    )
    (package / "agents/gigaagent.yaml").write_text("interface:\n  display_name: Пример\n")
    monkeypatch.setenv("OUROBOROS_REPO_DIR", str(tmp_path))
    user_settings_factory.reset_for_tests(None)
    registry_app = build_registry_app()
    owner = {OWNER_HEADER: "alice"}
    with TestClient(registry_app) as client:
        agent = client.post("/api/agents/", json={"name": "sub"}, headers=owner).json()["agent_id"]
        catalog_url = f"/api/owner-settings/{agent}/skills"
        before = client.get(catalog_url, headers=owner)
        assert before.status_code == 200
        assert not any(row["selected"] for row in before.json()["skills"])
        setting = f"/api/owner-settings/{agent}/OUROBOROS_USER_ENABLED_SKILL_IDS"
        assert (
            client.put(
                setting, json={"value": '["distribution:example", "distribution:removed"]'}, headers=owner
            ).status_code
            == 200
        )
        rows = client.get(catalog_url, headers=owner).json()["skills"]
        selected = [row for row in rows if row["selected"]]
        assert [row["id"] for row in selected] == ["distribution:example"]
        assert selected[0]["available"]
        assert selected[0]["display_name"] == "Пример"
        assert client.delete(setting, headers=owner).status_code == 200
        assert not any(row["selected"] for row in client.get(catalog_url, headers=owner).json()["skills"])
