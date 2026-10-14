from __future__ import annotations

import importlib.util
import pathlib
import shutil
from types import ModuleType

import pytest
from pydantic import ValidationError

from ouroboros.app.egress_auth_composition import EgressAuthComposition
from ouroboros.storage.settings.schema import UserHeadersAuth
from security_config.auth.header_collector import header_collector_declaration

SECURITY_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "security_config"


def _execute(path: pathlib.Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_declaration_parses_with_positive_ttls() -> None:
    assert header_collector_declaration.parameters
    assert all(parameter.ttl_sec > 0 for parameter in header_collector_declaration.parameters)


def test_every_name_the_example_polygon_forwards_is_declared(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_POLIGON", "EXAMPLE")

    servers = _execute(SECURITY_CONFIG / "mcp.py", "example_polygon_mcp").mcp_servers

    assert [server.id for server in servers if isinstance(server.auth, UserHeadersAuth)] == ["example-user-mcp"]
    assert EgressAuthComposition.undeclared_user_headers(servers, header_collector_declaration) == []


def test_every_name_the_example_a2a_list_forwards_is_declared(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_POLIGON", "EXAMPLE")

    agents = _execute(SECURITY_CONFIG / "a2a.py", "example_polygon_a2a").a2a_remote_agents

    assert [agent.id for agent in agents] == ["example-agent"]
    assert EgressAuthComposition.undeclared_user_headers(agents, header_collector_declaration) == []


def test_an_empty_declaration_file_is_refused(tmp_path) -> None:
    shutil.copy(SECURITY_CONFIG / "auth" / "header_collector.py", tmp_path / "header_collector.py")
    (tmp_path / "header_collector.yaml").write_text("", encoding="utf-8")

    with pytest.raises(ValidationError):
        _execute(tmp_path / "header_collector.py", "empty_header_collector")
