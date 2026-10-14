"""Settings round-trip tests for MCP — masking, rehydration, and env propagation."""

from __future__ import annotations

import json

import pytest
from mcp.client.session import DISCOVER_TIMEOUT_SECONDS

from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict


def test_settings_defaults_include_only_instance_mcp_keys(isolate_settings):
    cfg = isolate_settings
    assert "MCP_ENABLED" in cfg.SETTINGS_DEFAULTS
    assert "MCP_SERVERS" in cfg.SETTINGS_DEFAULTS
    assert cfg.SETTINGS_DEFAULTS["OUROBOROS_USER_ENABLED_SKILL_IDS"] == []
    assert "OUROBOROS_USER_ENABLED_MCP_IDS" in cfg.SETTINGS_DEFAULTS
    assert cfg.SETTINGS_DEFAULTS["MCP_ENABLED"] is False
    assert cfg.SETTINGS_DEFAULTS["MCP_SERVERS"] == []
    assert cfg.SETTINGS_DEFAULTS["OUROBOROS_USER_ENABLED_MCP_IDS"] == []
    assert "MCP_TOOL_TIMEOUT_SEC" in cfg.SETTINGS_DEFAULTS
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
            "auth": {"kind": "user_token", "auth_header": "Authorization", "auth_token": "Bearer secret"},
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
    assert servers[0]["auth"] == {"kind": "user_token", "auth_header": "Authorization", "auth_token": "Bearer secret"}
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


def test_settings_defaults_include_the_two_client_budgets(isolate_settings):
    """The two MCP budgets a deployment cannot read off the code, and their only written-down values.

    `ouroboros/mcp/` holds no copy of either default — it calls the resolvers — so these entries are
    the single source of truth, and a default changed here is changed everywhere.
    """
    cfg = isolate_settings
    assert cfg.SETTINGS_DEFAULTS["MCP_REQUEST_TIMEOUT_SEC"] == 30.0
    assert cfg.SETTINGS_DEFAULTS["MCP_STREAM_READ_TIMEOUT_SEC"] == 300.0


def test_the_client_budgets_fall_back_to_their_defaults_when_unset(isolate_settings, monkeypatch):
    """An unset or unparseable value coerces to `0.0`, and zero is worse than the default.

    A zero read budget fails the negotiation of every configured server at once, and a zero stream
    budget retires each session as soon as it is idle — both a harsher answer to one mistyped
    environment variable than leaving the documented number in place.
    """
    cfg = isolate_settings
    monkeypatch.delenv("MCP_REQUEST_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("MCP_STREAM_READ_TIMEOUT_SEC", raising=False)
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["MCP_REQUEST_TIMEOUT_SEC"] = 0
    payload["MCP_STREAM_READ_TIMEOUT_SEC"] = -1
    cfg.save_settings(payload, allow_elevation=True)

    assert cfg.get_mcp_request_timeout_sec() == 30.0
    assert cfg.get_mcp_stream_read_timeout_sec() == 300.0


def test_the_client_budgets_reach_the_client_from_the_environment(isolate_settings, monkeypatch):
    """The whole point of both keys: set them in the environment and the client uses them.

    The connect budget is asserted as a *derivation*, not as a number — it is computed from the
    request budget, so raising that raises the start-up budget with it. That is what makes the key
    safe to expose: a deployment cannot set a per-request timeout the negotiation no longer fits in.
    """
    from ouroboros.mcp import connection as connection_mod, transports as transports_mod
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    cfg = isolate_settings
    monkeypatch.setenv("MCP_REQUEST_TIMEOUT_SEC", "5")
    monkeypatch.setenv("MCP_STREAM_READ_TIMEOUT_SEC", "45")
    bootstrap_config(force=True)

    assert cfg.get_mcp_request_timeout_sec() == 5.0
    assert connection_mod.get_mcp_request_timeout_sec() == 5.0
    assert transports_mod.get_mcp_stream_read_timeout_sec() == 45.0
    assert connection_mod._connect_budget_sec() == pytest.approx(
        transports_mod.TCP_CONNECT_TIMEOUT_SEC + connection_mod._PROBE_PASSES * (DISCOVER_TIMEOUT_SECONDS + 5.0) + 5.0
    )


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


def test_user_enabled_skill_ids_are_an_instance_setting_with_a_normalized_env_value():
    settings = AppSettings.model_validate(
        {"OUROBOROS_USER_ENABLED_SKILL_IDS": '["distribution:alpha", " distribution:beta ", "distribution:alpha"]'}
    )

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS == ["distribution:alpha", "distribution:beta"]


@parametrize_from_dict(
    {
        "non-string item": {"value": '["distribution:alpha", 1]'},
        "malformed array": {"value": '["distribution:alpha"'},
        "unclosed json object": {"value": '{"skill":"distribution:alpha"'},
        "empty item": {"value": "distribution:alpha,,distribution:beta"},
    }
)
def test_user_enabled_skill_ids_reject_invalid_instance_values(value: str):
    with pytest.raises(ValueError, match="enabled skill ids"):
        AppSettings.model_validate({"OUROBOROS_USER_ENABLED_SKILL_IDS": value})


def test_mcp_user_enabled_mcp_ids_is_an_instance_setting():
    assert "OUROBOROS_USER_ENABLED_MCP_IDS" in AppSettings.model_fields


def test_mcp_user_enabled_mcp_ids_defaults_to_empty_list():
    assert AppSettings().OUROBOROS_USER_ENABLED_MCP_IDS == []


def test_mcp_user_enabled_mcp_ids_parses_env_json(isolate_settings, monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", '["search", "calendar"]')
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    assert isolate_settings.load_settings().OUROBOROS_USER_ENABLED_MCP_IDS == ["search", "calendar"]


def test_mcp_user_enabled_mcp_ids_parses_env_comma_separated(isolate_settings, monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", "search, calendar")
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    assert isolate_settings.load_settings().OUROBOROS_USER_ENABLED_MCP_IDS == ["search", "calendar"]


def test_mcp_user_enabled_mcp_ids_malformed_env_resolves_to_empty(isolate_settings, monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", '{"server": "search"}')
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    assert isolate_settings.load_settings().OUROBOROS_USER_ENABLED_MCP_IDS == []
