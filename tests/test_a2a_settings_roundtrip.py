"""Settings round-trip for outbound A2A — coercion, rehydration, and env propagation."""

from __future__ import annotations

import json


def _agent(**overrides):
    entry = {
        "id": "researcher",
        "name": "Research Agent",
        "enabled": True,
        "url": "https://agents.example/research",
        "auth_header": "Authorization",
        "auth_token": "Bearer secret",
        "featured_skills": ["literature_search"],
        "retrieval_aliases": ["research", "papers"],
        "tool_result_limit": 30_000,
        "poll_interval_sec": 1.5,
    }
    entry.update(overrides)
    return entry


def test_settings_defaults_are_off_and_empty(isolate_settings):
    cfg = isolate_settings
    assert cfg.SETTINGS_DEFAULTS["A2A_EGRESS_ENABLED"] is False
    assert cfg.SETTINGS_DEFAULTS["A2A_REMOTE_AGENTS"] == []
    assert cfg.SETTINGS_DEFAULTS["A2A_TOOL_TIMEOUT_SEC"] == 300


def test_round_trip_preserves_the_agent_list(isolate_settings):
    cfg = isolate_settings
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["A2A_EGRESS_ENABLED"] = True
    payload["A2A_REMOTE_AGENTS"] = [_agent()]
    payload["A2A_TOOL_TIMEOUT_SEC"] = 120
    cfg.save_settings(payload, allow_elevation=True)

    loaded = cfg.load_settings()
    assert loaded.A2A_EGRESS_ENABLED is True
    assert loaded.A2A_TOOL_TIMEOUT_SEC == 120
    agents = loaded.get("A2A_REMOTE_AGENTS")
    assert isinstance(agents, list)
    assert agents[0]["id"] == "researcher"
    assert agents[0]["auth_token"] == "Bearer secret"
    assert agents[0]["featured_skills"] == ["literature_search"]
    assert agents[0]["retrieval_aliases"] == ["research", "papers"]
    assert agents[0]["tool_result_limit"] == 30_000
    assert agents[0]["poll_interval_sec"] == 1.5


def test_coerce_setting_value_accepts_a_json_string(isolate_settings):
    cfg = isolate_settings
    raw = json.dumps([_agent(id="a")])
    coerced = cfg._coerce_setting_value("A2A_REMOTE_AGENTS", raw)
    assert isinstance(coerced, list)
    assert coerced and coerced[0]["id"] == "a"


def test_coerce_setting_value_drops_garbage(isolate_settings):
    cfg = isolate_settings
    assert cfg._coerce_setting_value("A2A_REMOTE_AGENTS", "not json") == []
    assert cfg._coerce_setting_value("A2A_REMOTE_AGENTS", None) == []
    assert cfg._coerce_setting_value("A2A_REMOTE_AGENTS", 123) == []


def test_a_malformed_entry_does_not_take_the_list_down(isolate_settings):
    cfg = isolate_settings
    coerced = cfg._coerce_setting_value(
        "A2A_REMOTE_AGENTS",
        [
            _agent(id="good"),
            # Out of range: settings validation rejects it, and it alone.
            _agent(id="bad", tool_result_limit=999_999_999),
            _agent(id="also-good"),
        ],
    )
    assert [entry["id"] for entry in coerced] == ["good", "also-good"]


def test_timeout_falls_back_to_the_default_when_unset(isolate_settings, monkeypatch):
    cfg = isolate_settings
    monkeypatch.delenv("A2A_TOOL_TIMEOUT_SEC", raising=False)
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["A2A_TOOL_TIMEOUT_SEC"] = 0  # invalid -> default
    cfg.save_settings(payload, allow_elevation=True)
    assert cfg.get_a2a_tool_timeout_sec() == cfg.SETTINGS_DEFAULTS["A2A_TOOL_TIMEOUT_SEC"]


def test_timeout_respects_env(isolate_settings, monkeypatch):
    cfg = isolate_settings
    monkeypatch.setenv("A2A_TOOL_TIMEOUT_SEC", "45")
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    assert cfg.get_a2a_tool_timeout_sec() == 45


def test_flag_is_read_live_rather_than_snapshotted(isolate_settings):
    cfg = isolate_settings
    payload = dict(cfg.SETTINGS_DEFAULTS)
    payload["A2A_EGRESS_ENABLED"] = True
    cfg.save_settings(payload, allow_elevation=True)
    assert cfg.get_a2a_egress_enabled() is True

    payload["A2A_EGRESS_ENABLED"] = False
    cfg.save_settings(payload, allow_elevation=True)
    assert cfg.get_a2a_egress_enabled() is False


def test_subprocess_env_carries_the_flag_but_never_the_agent_list(isolate_settings, monkeypatch):
    cfg = isolate_settings
    monkeypatch.delenv("A2A_REMOTE_AGENTS", raising=False)
    env = cfg.build_subprocess_env(
        {
            "A2A_EGRESS_ENABLED": True,
            "A2A_TOOL_TIMEOUT_SEC": 90,
            "A2A_REMOTE_AGENTS": [_agent()],
        },
        base={},
    )
    assert env.get("A2A_EGRESS_ENABLED") == "True"
    assert env.get("A2A_TOOL_TIMEOUT_SEC") == "90"
    # Agent entries carry credentials; a child process environment is the one place they
    # cannot be kept out of, so the list is never projected.
    assert "A2A_REMOTE_AGENTS" not in env


def test_apply_settings_to_env_does_not_serialize_the_agent_list(isolate_settings, monkeypatch, preserve_environ):
    cfg = isolate_settings
    monkeypatch.delenv("A2A_REMOTE_AGENTS", raising=False)
    cfg.apply_settings_to_env(
        {
            "A2A_EGRESS_ENABLED": True,
            "A2A_TOOL_TIMEOUT_SEC": 90,
            "A2A_REMOTE_AGENTS": [_agent()],
        }
    )
    import os

    assert os.environ.get("A2A_EGRESS_ENABLED") == "True"
    assert "A2A_REMOTE_AGENTS" not in os.environ
