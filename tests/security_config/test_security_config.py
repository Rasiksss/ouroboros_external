"""The security-owned server lists — polygon branching, legacy delegation, secrets from env."""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    EgressAuthKind,
    McpServerEntry,
    MTLSAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from tests.conftest import refresh_test_config

_MODULES = ("security_config.mcp", "security_config.a2a")


@pytest.fixture
def load_security_config(monkeypatch):
    """Re-import a security_config module against a patched env, and undo it afterwards.

    The modules build their lists at import time, so a test that changes the polygon has to
    reload them — and has to put back what the rest of the session imported.
    """
    snapshot = dict(os.environ)

    def _load(module_name: str, **env: str):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(importlib.import_module(module_name))

    yield _load

    os.environ.clear()
    os.environ.update(snapshot)
    refresh_test_config()
    for name in _MODULES:
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)


def _mcp_entry(**overrides) -> dict:
    entry = {"id": "alpha", "url": "https://alpha.example/mcp", "enabled": True}
    entry.update(overrides)
    return entry


def _a2a_entry(**overrides) -> dict:
    entry = {"id": "researcher", "url": "https://research.example", "enabled": True}
    entry.update(overrides)
    return entry


def test_dev_polygon_delegates_mcp_to_settings(load_security_config):
    module = load_security_config(
        "security_config.mcp",
        OUROBOROS_POLIGON="DEV",
        MCP_SERVERS=json.dumps([_mcp_entry(), _mcp_entry(id="beta")]),
    )

    # Flat, one entry per configured server: an earlier draft appended the whole list into a list.
    assert [entry.id for entry in module.mcp_servers] == ["alpha", "beta"]
    assert all(isinstance(entry, McpServerEntry) for entry in module.mcp_servers)


def test_dev_polygon_delegates_a2a_to_settings(load_security_config):
    module = load_security_config(
        "security_config.a2a",
        OUROBOROS_POLIGON="DEV",
        A2A_REMOTE_AGENTS=json.dumps([_a2a_entry(), _a2a_entry(id="writer")]),
    )

    assert [entry.id for entry in module.a2a_remote_agents] == ["researcher", "writer"]
    assert all(isinstance(entry, A2aRemoteAgentEntry) for entry in module.a2a_remote_agents)


def test_dev_polygon_without_settings_yields_no_mcp_servers(load_security_config):
    module = load_security_config("security_config.mcp", OUROBOROS_POLIGON="DEV", MCP_SERVERS="[]")

    assert module.mcp_servers == []


def test_dev_polygon_without_settings_yields_no_a2a_agents(load_security_config):
    module = load_security_config("security_config.a2a", OUROBOROS_POLIGON="DEV", A2A_REMOTE_AGENTS="[]")

    assert module.a2a_remote_agents == []


def _by_id(entries, entry_id):
    (entry,) = [entry for entry in entries if entry.id == entry_id]
    return entry


def test_example_polygon_gets_the_mcp_example_with_a_token_from_env(load_security_config):
    module = load_security_config(
        "security_config.mcp",
        OUROBOROS_POLIGON="EXAMPLE",
        MCP_EXAMPLE_TOKEN="mcp-secret",
    )

    entry = _by_id(module.mcp_servers, "example-mcp")
    assert isinstance(entry.auth, UserTokenAuth)
    assert entry.auth.auth_header == "X-API-Key"
    assert entry.auth.auth_token == "mcp-secret"


def test_example_polygon_gets_one_mcp_example_per_credentialed_kind(load_security_config):
    module = load_security_config("security_config.mcp", OUROBOROS_POLIGON="EXAMPLE", MCP_EXAMPLE_TOKEN="mcp-secret")

    assert {entry.id: type(entry.auth) for entry in module.mcp_servers} == {
        "example-mcp": UserTokenAuth,
        "example-user-mcp": UserHeadersAuth,
        "example-mtls-mcp": MTLSAuth,
    }
    assert [entry.id for entry in module.mcp_servers if entry.url.startswith("http://")] == ["example-mtls-mcp"]
    user_headers = _by_id(module.mcp_servers, "example-user-mcp").auth
    assert isinstance(user_headers, UserHeadersAuth)
    assert (user_headers.headers, user_headers.cookies) == (["X-Sudir-Token"], ["sudir_session"])


def test_the_a2a_example_declares_its_authorization(load_security_config):
    module = load_security_config(
        "security_config.a2a",
        OUROBOROS_POLIGON="EXAMPLE",
        A2A_EXAMPLE_TOKEN="a2a-secret",
    )

    (entry,) = module.a2a_remote_agents
    assert entry.id == "example-agent"
    assert entry.url.startswith("https://")
    assert isinstance(entry.auth, UserTokenAuth)
    assert (entry.auth.kind, entry.auth.auth_header, entry.auth.auth_token) == (
        EgressAuthKind.USER_TOKEN,
        "Authorization",
        "a2a-secret",
    )


def test_example_polygon_ignores_legacy_mcp_settings(load_security_config):
    module = load_security_config(
        "security_config.mcp",
        OUROBOROS_POLIGON="EXAMPLE",
        MCP_SERVERS=json.dumps([_mcp_entry()]),
    )

    assert [entry.id for entry in module.mcp_servers] == ["example-mcp", "example-user-mcp", "example-mtls-mcp"]


def test_example_polygon_ignores_legacy_a2a_settings(load_security_config):
    module = load_security_config(
        "security_config.a2a",
        OUROBOROS_POLIGON="EXAMPLE",
        A2A_REMOTE_AGENTS=json.dumps([_a2a_entry()]),
    )

    assert [entry.id for entry in module.a2a_remote_agents] == ["example-agent"]


def test_unconfigured_polygon_yields_no_mcp_servers(load_security_config):
    # A polygon with no branch of its own gets nothing — not the example, and not the legacy
    # list either, which only the DEV branch reads.
    module = load_security_config(
        "security_config.mcp",
        OUROBOROS_POLIGON="PROM",
        MCP_SERVERS=json.dumps([_mcp_entry()]),
    )

    assert module.mcp_servers == []


def test_unconfigured_polygon_yields_no_a2a_agents(load_security_config):
    module = load_security_config(
        "security_config.a2a",
        OUROBOROS_POLIGON="PROM",
        A2A_REMOTE_AGENTS=json.dumps([_a2a_entry()]),
    )

    assert module.a2a_remote_agents == []


def test_the_mcp_example_carries_no_secret_of_its_own(load_security_config, monkeypatch):
    monkeypatch.delenv("MCP_EXAMPLE_TOKEN", raising=False)

    module = load_security_config("security_config.mcp", OUROBOROS_POLIGON="EXAMPLE")

    assert [entry.auth.auth_token for entry in module.mcp_servers if isinstance(entry.auth, UserTokenAuth)] == [None]


def test_the_a2a_example_carries_no_secret_of_its_own(load_security_config, monkeypatch):
    monkeypatch.delenv("A2A_EXAMPLE_TOKEN", raising=False)

    module = load_security_config("security_config.a2a", OUROBOROS_POLIGON="EXAMPLE")

    (entry,) = module.a2a_remote_agents
    assert isinstance(entry.auth, UserTokenAuth)
    assert entry.auth.auth_token is None
