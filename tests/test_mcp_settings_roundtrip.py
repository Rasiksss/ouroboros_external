"""Settings round-trip tests for MCP — masking, rehydration, and env propagation."""

from __future__ import annotations

import json


def test_settings_defaults_include_mcp_keys(isolate_settings):
    cfg = isolate_settings
    assert "MCP_ENABLED" in cfg.SETTINGS_DEFAULTS
    assert "MCP_SERVERS" in cfg.SETTINGS_DEFAULTS
    assert "MCP_TOOL_TIMEOUT_SEC" in cfg.SETTINGS_DEFAULTS
    assert cfg.SETTINGS_DEFAULTS["MCP_ENABLED"] is False
    assert cfg.SETTINGS_DEFAULTS["MCP_SERVERS"] == []
    assert cfg.SETTINGS_DEFAULTS["MCP_TOOL_TIMEOUT_SEC"] == 60


def test_settings_round_trip_preserves_servers_list(isolate_settings):
    cfg = isolate_settings
    servers = [
        {
            "id": "github",
            "name": "GitHub",
            "enabled": True,
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "auth_header": "Authorization",
            "auth_token": "Bearer secret",
            "allowed_tools": ["search_repos"],
            "retrieval_aliases": ["github", "repositories"],
            "tool_result_limits": {"search_repos": 30_000},
        }
    ]
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["MCP_ENABLED"] = True
    payload["MCP_SERVERS"] = servers
    payload["MCP_TOOL_TIMEOUT_SEC"] = 90
    cfg.save_settings(payload, allow_elevation=True)

    loaded = cfg.load_settings()
    assert loaded.MCP_ENABLED is True
    assert loaded.MCP_TOOL_TIMEOUT_SEC == 90
    servers = loaded.get("MCP_SERVERS")
    assert isinstance(servers, list)
    assert servers[0]["id"] == "github"
    assert servers[0]["auth_token"] == "Bearer secret"
    assert servers[0]["retrieval_aliases"] == ["github", "repositories"]
    assert servers[0]["tool_result_limits"] == {"search_repos": 30_000}


def test_get_mcp_tool_timeout_falls_back_to_default(isolate_settings, monkeypatch):
    cfg = isolate_settings
    monkeypatch.delenv("MCP_TOOL_TIMEOUT_SEC", raising=False)
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["MCP_TOOL_TIMEOUT_SEC"] = 0  # invalid -> default
    cfg.save_settings(payload, allow_elevation=True)
    assert cfg.get_mcp_tool_timeout_sec() == cfg.SETTINGS_DEFAULTS["MCP_TOOL_TIMEOUT_SEC"]


def test_get_mcp_tool_timeout_respects_env(isolate_settings, monkeypatch):
    cfg = isolate_settings
    monkeypatch.setenv("MCP_TOOL_TIMEOUT_SEC", "120")
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    assert cfg.get_mcp_tool_timeout_sec() == 120


def test_apply_settings_to_env_does_not_serialize_servers_list(isolate_settings, monkeypatch, preserve_environ):
    cfg = isolate_settings
    monkeypatch.delenv("MCP_SERVERS", raising=False)
    cfg.apply_settings_to_env(
        {
            "MCP_ENABLED": True,
            "MCP_TOOL_TIMEOUT_SEC": 45,
            "MCP_SERVERS": [{"id": "x", "url": "https://e.example/mcp"}],
        }
    )
    import os

    assert os.environ.get("MCP_ENABLED") == "True"
    assert os.environ.get("MCP_TOOL_TIMEOUT_SEC") == "45"
    # MCP_SERVERS is intentionally excluded from env propagation —
    # it stays a list-of-dicts read via load_settings().
    assert "MCP_SERVERS" not in os.environ


def test_coerce_setting_value_handles_string_payload(isolate_settings):
    cfg = isolate_settings
    raw = json.dumps([{"id": "a", "url": "https://e.example/mcp"}])
    coerced = cfg._coerce_setting_value("MCP_SERVERS", raw)
    assert isinstance(coerced, list)
    assert coerced and coerced[0]["id"] == "a"


def test_coerce_setting_value_handles_garbage_payload(isolate_settings):
    cfg = isolate_settings
    assert cfg._coerce_setting_value("MCP_SERVERS", "not json") == []
    assert cfg._coerce_setting_value("MCP_SERVERS", None) == []
    assert cfg._coerce_setting_value("MCP_SERVERS", 123) == []
