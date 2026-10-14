"""Pydantic AppSettings validation and env-only store."""

from __future__ import annotations

import json
import logging
import os

import pytest
from pydantic import SecretStr, ValidationError

from ouroboros.storage.settings.profile import apply_production_overlay, is_production
from ouroboros.storage.settings.schema import (
    AppSettings,
    _coerce_a2a_remote_agents,
    _coerce_mcp_servers,
)
from ouroboros.storage.settings.store import (
    apply_settings_to_env,
    build_subprocess_env,
    load_settings,
    reset_memory_settings_for_tests,
    save_settings,
)
from tests.helpers.parametrize import parametrize_from_dict


@pytest.mark.parametrize("kind,default", [("DOCUMENT", 50 * 1024 * 1024), ("IMAGE", 20 * 1024 * 1024)])
def test_attachment_limits_default_and_env(kind, default, monkeypatch, isolate_settings):
    key = f"OUROBOROS_ATTACHMENT_{kind}_MAX_BYTES"
    assert getattr(AppSettings(), key) == default
    monkeypatch.setenv(key, "12345")
    reset_memory_settings_for_tests()
    assert getattr(load_settings(), key) == 12345


@pytest.mark.parametrize("kind", ["DOCUMENT", "IMAGE"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "0", "-1", "1.5", True, 1.0, "1.0"])
def test_attachment_limits_require_positive_integers(kind, value):
    key = f"OUROBOROS_ATTACHMENT_{kind}_MAX_BYTES"
    with pytest.raises(ValidationError, match=key):
        AppSettings.model_validate({key: value})


@pytest.mark.parametrize("suffix,value", [("PARSER_JOIN_TIMEOUT_SEC", 0), ("PARSE_TIMEOUT_SEC", 1.5)])
def test_attachment_parser_budgets_reject_invalid_values(suffix, value):
    key = f"OUROBOROS_ATTACHMENT_{suffix}"
    with pytest.raises(ValidationError, match=key):
        AppSettings.model_validate({key: value})


def test_mcp_servers_coerce_from_json_string():
    raw = json.dumps([{"id": "x", "url": "https://example.com/mcp", "enabled": True}])
    servers = _coerce_mcp_servers(raw)
    assert len(servers) == 1
    assert servers[0].id == "x"


def test_mcp_servers_drop_invalid_entries():
    servers = _coerce_mcp_servers([{"id": "ok", "url": "https://e.example/mcp"}, "bad", None])
    assert len(servers) == 1


def test_mcp_servers_drop_an_entry_with_no_url():
    """The settings path is where a URL-less entry has to die: both transports need one."""
    assert _coerce_mcp_servers([{"id": "no-url", "transport": "streamable_http"}]) == []


def test_a_dropped_mcp_entry_is_named(caplog):
    """A drop nobody sees empties `MCP_SERVERS`, and an empty list skips manager initialization.

    So one entry written for the previous shape takes *every* MCP tool out of the catalogue, and
    the only trace of it is an absence. `url` being required and `extra="forbid"` make that the
    ordinary outcome of an upgrade rather than a rare one.
    """
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        assert _coerce_mcp_servers([{"id": "legacy", "command": "/usr/bin/server", "args": []}]) == []

    reported = [record.getMessage() for record in caplog.records]
    assert any("legacy" in message for message in reported), reported


def test_a_dropped_mcp_entry_does_not_carry_its_own_values_into_the_log(caplog):
    """The entry is where an operator keeps `auth_token`, and pydantic's text embeds what it rejected."""
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        _coerce_mcp_servers([{"id": "legacy", "url": "https://e.example/mcp", "auth_tokn": "s3cr3t-value"}])

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "auth_tokn" in rendered, "the rejected key has to be named or the drop is unactionable"
    assert "s3cr3t-value" not in rendered


def test_an_unparseable_mcp_servers_string_is_reported_without_its_value(caplog):
    """The same silent emptying, reached through the env var rather than through one entry."""
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        assert _coerce_mcp_servers('[{"id": "x", "auth_token": "s3cr3t-value"') == []

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "MCP_SERVERS" in rendered
    assert "s3cr3t-value" not in rendered


def test_a_dropped_a2a_entry_is_named(caplog):
    """The other configured list of external tool sources, silent for the same reason.

    A dropped remote agent is one tool gone from the catalogue and an empty list is no A2A egress
    at all — the argument that made `_coerce_mcp_servers` name its drops holds here word for word,
    so a fix applied to one list and not the other leaves half the defect.
    """
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        assert _coerce_a2a_remote_agents([{"id": "helper", "poll_interval_sec": "often"}]) == []

    reported = [record.getMessage() for record in caplog.records]
    assert any("helper" in message for message in reported), reported


def test_a_dropped_a2a_entry_does_not_carry_its_own_values_into_the_log(caplog):
    """An A2A entry carries `auth_token` exactly as an MCP one does."""
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        _coerce_a2a_remote_agents([{"id": "helper", "tool_result_limit": "s3cr3t-value"}])

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "tool_result_limit" in rendered, "the rejected key has to be named or the drop is unactionable"
    assert "s3cr3t-value" not in rendered


def test_an_unparseable_a2a_remote_agents_string_is_reported_without_its_value(caplog):
    with caplog.at_level(logging.WARNING, logger="ouroboros.storage.settings.schema"):
        assert _coerce_a2a_remote_agents('[{"id": "x", "auth_token": "s3cr3t-value"') == []

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "A2A_REMOTE_AGENTS" in rendered
    assert "s3cr3t-value" not in rendered


def test_custom_secret_key_allowed():
    settings = AppSettings.model_validate({"GITHUB_TOKEN": "ghp_test"})
    assert settings.get("GITHUB_TOKEN") == "ghp_test"


def test_background_llm_timeout_is_typed():
    settings = AppSettings(OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC="45")
    assert settings.OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC == 45.0


def test_log_format_and_sink_tolerate_invalid_env_values():
    # A typo in the deploy config must fall back to defaults, not crash bootstrap.
    settings = AppSettings(OUROBOROS_LOG_FORMAT="Fancy", OUROBOROS_LOG_SINK="syslog")
    assert settings.OUROBOROS_LOG_FORMAT == "json"
    assert settings.OUROBOROS_LOG_SINK == "stdout"
    settings = AppSettings(OUROBOROS_LOG_FORMAT=" TEXT ", OUROBOROS_LOG_SINK=" File ")
    assert settings.OUROBOROS_LOG_FORMAT == "text"
    assert settings.OUROBOROS_LOG_SINK == "file"
    assert AppSettings(OUROBOROS_LOG_ROTATION="hourly").OUROBOROS_LOG_ROTATION == "size"
    assert AppSettings(OUROBOROS_LOG_ROTATION=" Time ").OUROBOROS_LOG_ROTATION == "time"


def test_log_rotation_limits_fall_back_to_safe_defaults_not_zero():
    """0 is the dangerous value here: it disables rotation. Garbage must not reach it."""
    settings = AppSettings(OUROBOROS_LOG_FILE_MAX_BYTES="50MB", OUROBOROS_LOG_FILE_BACKUP_COUNT="many")
    assert settings.OUROBOROS_LOG_FILE_MAX_BYTES == 52_428_800
    assert settings.OUROBOROS_LOG_FILE_BACKUP_COUNT == 5
    # Only an explicit 0 may mean "rotation off"; a negative is garbage too.
    assert AppSettings(OUROBOROS_LOG_FILE_MAX_BYTES="0").OUROBOROS_LOG_FILE_MAX_BYTES == 0
    assert AppSettings(OUROBOROS_LOG_FILE_MAX_BYTES="-1").OUROBOROS_LOG_FILE_MAX_BYTES == 52_428_800
    assert AppSettings(OUROBOROS_LOG_FILE_BACKUP_COUNT="-3").OUROBOROS_LOG_FILE_BACKUP_COUNT == 5
    # 0 means "keep every file" under time rotation; the size handler floors it.
    assert AppSettings(OUROBOROS_LOG_FILE_BACKUP_COUNT="0").OUROBOROS_LOG_FILE_BACKUP_COUNT == 0


def test_blank_log_dir_falls_back_to_default():
    assert AppSettings(OUROBOROS_LOG_DIR="  ").OUROBOROS_LOG_DIR == "/var/log/app"


def test_llm_claim_poll_interval_is_typed_and_bounded():
    key = "OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC"
    assert AppSettings().OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC == 1.0
    assert AppSettings.model_validate({key: "0.5"}).OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC == 0.5
    for invalid in (0, -1, 0.001, 31, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            AppSettings.model_validate({key: invalid})


def test_durable_job_limits_have_safe_defaults_and_validate_together():
    settings = AppSettings()
    assert settings.OUROBOROS_DURABLE_JOBS_ENABLED is False
    assert settings.OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER == 5
    assert settings.OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER == 3

    for field in (
        "OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER",
        "OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER",
    ):
        with pytest.raises(ValidationError):
            AppSettings.model_validate({field: 0})

    with pytest.raises(ValidationError, match="running root jobs.*active root jobs"):
        AppSettings(
            OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER=2,
            OUROBOROS_MAX_RUNNING_ROOT_JOBS_PER_OWNER=3,
        )


def test_rejects_lowercase_custom_keys():
    with pytest.raises(ValueError, match="Invalid settings key"):
        AppSettings.model_validate({"github_token": "x"})


def test_production_overlay_preserves_mcp_enabled(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    base = AppSettings(MCP_ENABLED=True)
    overlay = apply_production_overlay(base)
    assert overlay.MCP_ENABLED is True


def test_never_writes_settings_file(tmp_path, monkeypatch):
    import ouroboros.app.config as cfg

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "settings.json"
    settings_path.write_text('{"OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Pro"}', encoding="utf-8")
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path)
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_MODEL", "gigachat-b2bank::GigaChat")
    reset_memory_settings_for_tests()

    loaded = load_settings()
    assert loaded.OUROBOROS_MODEL.startswith("gigachat-b2bank::GigaChat")
    save_settings(loaded.model_copy(update={"MCP_TOOL_TIMEOUT_SEC": 42}))
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["OUROBOROS_MODEL"] == "gigachat-b2bank::GigaChat-Pro"
    assert load_settings().MCP_TOOL_TIMEOUT_SEC == 42


def test_env_overrides_bool_default_false(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("A2A_ENABLED", "true")
    reset_memory_settings_for_tests()
    assert load_settings().A2A_ENABLED is True


def test_time_and_retrieval_settings_are_typed(monkeypatch):
    monkeypatch.setenv("OUROBOROS_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setenv("OUROBOROS_LOCALE", "ru-RU")
    monkeypatch.setenv("OUROBOROS_CONTEXT_RETRIEVAL_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_EPISODE_RETRIEVAL_TOP_K", "7")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS", "123")
    reset_memory_settings_for_tests()

    settings = load_settings()

    assert settings.OUROBOROS_TIMEZONE == "Asia/Tokyo"
    assert settings.OUROBOROS_LOCALE == "ru-RU"
    assert settings.OUROBOROS_CONTEXT_RETRIEVAL_ENABLED is False
    assert settings.OUROBOROS_EPISODE_RETRIEVAL_TOP_K == 7
    assert settings.OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS == 123


def test_slow_llm_status_settings_default_on_and_are_typed(monkeypatch):
    """On unless an operator says otherwise: an agent that looks dead is the
    worse failure. The kill switch and the threshold must both survive the env
    round-trip with their types intact."""
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_SLOW_LLM_STATUS_ENABLED is True
    assert load_settings().OUROBOROS_SLOW_LLM_STATUS_SEC == 30

    # 'maybe' is the point: pydantic rejects it, our bool validator maps it to
    # the default. Using 'no' would pass with the validator unregistered.
    monkeypatch.setenv("OUROBOROS_SLOW_LLM_STATUS_ENABLED", "maybe")
    monkeypatch.setenv("OUROBOROS_SLOW_LLM_STATUS_SEC", "45")
    reset_memory_settings_for_tests()

    settings = load_settings()
    assert settings.OUROBOROS_SLOW_LLM_STATUS_ENABLED is False
    assert settings.OUROBOROS_SLOW_LLM_STATUS_SEC == 45


def test_reasoning_progress_defaults_on_and_survives_the_env_round_trip(monkeypatch):
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_REASONING_PROGRESS_ENABLED is True

    monkeypatch.setenv("OUROBOROS_REASONING_PROGRESS_ENABLED", "false")
    reset_memory_settings_for_tests()

    assert load_settings().OUROBOROS_REASONING_PROGRESS_ENABLED is False


def test_slow_llm_status_seconds_survive_a_malformed_value(monkeypatch):
    """A typo must not throw: it coerces to 0, which the adapter reads as
    'use the default' rather than 'notify after one second'."""
    monkeypatch.setenv("OUROBOROS_SLOW_LLM_STATUS_SEC", "30s")
    reset_memory_settings_for_tests()

    assert load_settings().OUROBOROS_SLOW_LLM_STATUS_SEC == 0


def test_browser_ingress_enabled_defaults_false_and_env_override(monkeypatch):
    reset_memory_settings_for_tests()
    assert load_settings().BROWSER_INGRESS_ENABLED is False
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "true")
    reset_memory_settings_for_tests()
    assert load_settings().BROWSER_INGRESS_ENABLED is True


def test_multiuser_defaults_false_and_env_override(monkeypatch):
    """Off unless a deployment declares itself multi-user: the default must never be trust."""
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_MULTIUSER is False
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_MULTIUSER is True


def test_browser_base_url_defaults_empty_and_env_override(monkeypatch):
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_BROWSER_BASE_URL == ""
    monkeypatch.setenv("OUROBOROS_BROWSER_BASE_URL", "http://127.0.0.1:8888")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_BROWSER_BASE_URL == "http://127.0.0.1:8888"


def test_user_file_ttl_settings_defaults_and_env_override(monkeypatch):
    reset_memory_settings_for_tests()
    defaults = load_settings()
    assert defaults.OUROBOROS_USER_FILE_TTL_HOURS == 720
    assert defaults.OUROBOROS_JOB_SCRATCH_TTL_HOURS == 24

    monkeypatch.setenv("OUROBOROS_USER_FILE_TTL_HOURS", "12")
    monkeypatch.setenv("OUROBOROS_JOB_SCRATCH_TTL_HOURS", "2")
    reset_memory_settings_for_tests()

    loaded = load_settings()
    assert loaded.OUROBOROS_USER_FILE_TTL_HOURS == 12
    assert loaded.OUROBOROS_JOB_SCRATCH_TTL_HOURS == 2
    assert build_subprocess_env(loaded, base={})["OUROBOROS_USER_FILE_TTL_HOURS"] == "12"


def test_vision_model_defaults_empty_env_override_and_subprocess_projection(monkeypatch):
    monkeypatch.delenv("OUROBOROS_MODEL_VISION", raising=False)
    reset_memory_settings_for_tests()
    defaults = load_settings()
    # Empty by default: no routing, main model is assumed multimodal.
    assert defaults.OUROBOROS_MODEL_VISION == ""
    # Empty value is not projected into the subprocess env.
    assert "OUROBOROS_MODEL_VISION" not in build_subprocess_env(defaults, base={})

    monkeypatch.setenv("OUROBOROS_MODEL_VISION", "cloudru::Qwen-VL")
    reset_memory_settings_for_tests()

    loaded = load_settings()
    assert loaded.OUROBOROS_MODEL_VISION == "cloudru::Qwen-VL"
    assert build_subprocess_env(loaded, base={})["OUROBOROS_MODEL_VISION"] == "cloudru::Qwen-VL"


def test_production_loads_cloudru_foundation_models_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru-key")
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_BASE_URL", "https://cloudru.example/v1")
    reset_memory_settings_for_tests()

    loaded = load_settings()
    api_key = loaded.get("CLOUDRU_FOUNDATION_MODELS_API_KEY")
    if hasattr(api_key, "get_secret_value"):
        api_key = api_key.get_secret_value()
    assert api_key == "cloudru-key"
    assert loaded.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL") == "https://cloudru.example/v1"


def test_loads_dialogs_user_id_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("DIALOGS_USER_ID", "user-42")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.DIALOGS_USER_ID == "user-42"


def test_dialogs_optional_ux_features_are_disabled_by_default():
    settings = AppSettings()

    assert settings.DIALOGS_FILE_URL_PORT == 80
    assert settings.DIALOGS_REPLY_ENABLED is False
    assert settings.DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED is False
    assert settings.DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED is False
    assert settings.DIALOGS_HISTORY_RECOVERY_ENABLED is False
    assert settings.DIALOGS_HISTORY_RECOVERY_LIMIT == 10
    assert settings.DIALOGS_FORWARDING_DISABLED is False
    assert settings.DIALOGS_TYPING_ENABLED is False
    assert settings.DIALOGS_PARTICIPANT_IDENTITY_ENABLED is False
    assert settings.DIALOGS_BOT_NICK == ""


def test_gigachat_preview_canary_is_off_by_default():
    settings = AppSettings()

    assert settings.GIGACHAT_PREVIEW_ENABLED is False
    assert settings.GIGACHAT_PREVIEW_PROBABILITY == 0.0
    # No default name: the version under test is named by the operator or not checked.
    assert settings.GIGACHAT_PREVIEW_MODEL == ""


@pytest.mark.parametrize(
    ("configured", "effective"),
    [
        (0.01, 0.01),
        (0.05, 0.05),
        # Above the requirement's hard ceiling: clamped, never rejected, so a
        # fat-fingered deploy value cannot send half the traffic to preview.
        (0.5, 0.05),
        (1, 0.05),
        (-0.1, 0.0),
        # NaN compares false against everything, so min/max would pass it through
        # into the selector's int() and crash every task.
        ("nan", 0.0),
        (float("nan"), 0.0),
        (float("inf"), 0.05),
    ],
)
def test_gigachat_preview_probability_is_capped_at_five_percent(configured, effective):
    settings = AppSettings(GIGACHAT_PREVIEW_PROBABILITY=configured)

    assert pytest.approx(effective) == settings.GIGACHAT_PREVIEW_PROBABILITY


@pytest.mark.parametrize("value", [0, 101])
def test_dialogs_history_recovery_limit_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="DIALOGS_HISTORY_RECOVERY_LIMIT"):
        AppSettings(DIALOGS_HISTORY_RECOVERY_LIMIT=value)


@parametrize_from_dict({"negative": {"value": -1}, "above the shared hard max": {"value": 120_001}})
def test_skill_exec_result_limit_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="OUROBOROS_SKILL_EXEC_RESULT_LIMIT"):
        AppSettings(OUROBOROS_SKILL_EXEC_RESULT_LIMIT=value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT", 0),
        ("OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT", 1_001),
        ("OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC", 0.5),
        ("OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC", 3601),
        ("OUROBOROS_SCHEDULER_POLL_JITTER_SEC", -1),
        ("OUROBOROS_SCHEDULER_POLL_JITTER_SEC", 601),
    ],
)
def test_scheduler_knobs_reject_out_of_range(key, value):
    """Deployment knobs, so a mistyped value is loud at start-up rather than silently clamped."""
    with pytest.raises(ValueError, match=key):
        AppSettings(**{key: value})


@pytest.mark.parametrize(
    "key",
    [
        "OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT",
        "OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC",
        "OUROBOROS_SCHEDULER_POLL_JITTER_SEC",
    ],
)
@pytest.mark.parametrize("blank", ["", " ", "\n"])
def test_a_blank_scheduler_knob_keeps_its_default(key, blank):
    """A secret file or a rendered template arrives blank; it must not fail the whole config."""
    assert getattr(AppSettings(**{key: blank}), key) == getattr(AppSettings(), key)


def test_scheduler_knobs_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT", "50")
    monkeypatch.setenv("OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC", "5")
    monkeypatch.setenv("OUROBOROS_SCHEDULER_POLL_JITTER_SEC", "0")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT == 50
    assert loaded.OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC == 5.0
    assert loaded.OUROBOROS_SCHEDULER_POLL_JITTER_SEC == 0.0


def test_context_retrieval_and_calendar_settings_are_typed(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("OUROBOROS_LOCALE", "ru-RU")
    monkeypatch.setenv("OUROBOROS_CONTEXT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_EPISODE_RETRIEVAL_TOP_K", "7")
    monkeypatch.setenv("OUROBOROS_EPISODE_CARD_MAX_CHARS", "1200")
    monkeypatch.setenv("OUROBOROS_EPISODE_CONTEXT_TOKENS", "9000")
    monkeypatch.setenv("OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES", "50")
    monkeypatch.setenv("OUROBOROS_EPISODE_RETENTION_DAYS", "90")
    monkeypatch.setenv("OUROBOROS_TOOL_SUMMARY_RETENTION_DAYS", "30")
    monkeypatch.setenv("OUROBOROS_TOOL_RAW_RETENTION_DAYS", "0")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS", "24")
    monkeypatch.setenv("OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS", "5000")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.OUROBOROS_TIMEZONE == "Europe/Moscow"
    assert loaded.OUROBOROS_LOCALE == "ru-RU"
    assert loaded.OUROBOROS_CONTEXT_RETRIEVAL_ENABLED is True
    assert loaded.OUROBOROS_EPISODE_RETRIEVAL_TOP_K == 7
    assert loaded.OUROBOROS_EPISODE_CARD_MAX_CHARS == 1200
    assert loaded.OUROBOROS_EPISODE_CONTEXT_TOKENS == 9000
    assert loaded.OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES == 50
    assert loaded.OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS == 5000


def test_apply_settings_to_env_includes_dialogs_user_id(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.delenv("DIALOGS_USER_ID", raising=False)

    apply_settings_to_env(AppSettings(DIALOGS_USER_ID="user-42"))

    assert os.environ.get("DIALOGS_USER_ID") == "user-42"


def test_selfhosted_tls_settings_are_typed_and_projected(monkeypatch):
    monkeypatch.setitem(os.environ, "OUROBOROS_ENV", "production")
    monkeypatch.setitem(os.environ, "SELFHOSTED_LLM_CERT_PATH", "/certs/client.pem")
    monkeypatch.setitem(os.environ, "SELFHOSTED_LLM_KEY_PATH", "/certs/client.key")
    monkeypatch.setitem(os.environ, "SELFHOSTED_LLM_CA_PATH", "/certs/ca.pem")
    monkeypatch.setitem(os.environ, "SELFHOSTED_LLM_VERIFY_SSL", "false")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.SELFHOSTED_LLM_CERT_PATH == "/certs/client.pem"
    assert loaded.SELFHOSTED_LLM_KEY_PATH == "/certs/client.key"
    assert loaded.SELFHOSTED_LLM_CA_PATH == "/certs/ca.pem"
    assert loaded.SELFHOSTED_LLM_VERIFY_SSL is False

    apply_settings_to_env(loaded)
    assert os.environ.get("SELFHOSTED_LLM_CERT_PATH") == "/certs/client.pem"
    assert os.environ.get("SELFHOSTED_LLM_KEY_PATH") == "/certs/client.key"
    assert os.environ.get("SELFHOSTED_LLM_CA_PATH") == "/certs/ca.pem"
    assert os.environ.get("SELFHOSTED_LLM_VERIFY_SSL") == "False"


def test_gigasearch_settings_are_typed_and_projected(monkeypatch):
    monkeypatch.setitem(os.environ, "GIGASEARCH_BASE_URL", "https://gigasearch.local")
    monkeypatch.setitem(os.environ, "GIGASEARCH_USER_ID", "user-42")
    monkeypatch.setitem(os.environ, "GIGASEARCH_SURFACE_ID", "surface-7")
    monkeypatch.setitem(os.environ, "GIGASEARCH_API_KEY", "secret")
    monkeypatch.setitem(os.environ, "GIGASEARCH_TIMEOUT_SEC", "45")
    monkeypatch.setitem(os.environ, "GIGASEARCH_CERT_PATH", "/certs/client.pem")
    monkeypatch.setitem(os.environ, "GIGASEARCH_KEY_PATH", "/certs/client.key")
    monkeypatch.setitem(os.environ, "GIGASEARCH_CA_PATH", "/certs/ca.pem")
    monkeypatch.setitem(os.environ, "GIGASEARCH_VERIFY_SSL", "false")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.GIGASEARCH_BASE_URL == "https://gigasearch.local"
    assert loaded.GIGASEARCH_USER_ID == "user-42"
    assert loaded.GIGASEARCH_SURFACE_ID == "surface-7"
    assert loaded.get("GIGASEARCH_API_KEY") == "secret"
    assert loaded.GIGASEARCH_TIMEOUT_SEC == 45.0
    assert loaded.GIGASEARCH_CERT_PATH == "/certs/client.pem"
    assert loaded.GIGASEARCH_KEY_PATH == "/certs/client.key"
    assert loaded.GIGASEARCH_CA_PATH == "/certs/ca.pem"
    assert loaded.GIGASEARCH_VERIFY_SSL is False

    apply_settings_to_env(loaded)
    assert os.environ.get("GIGASEARCH_API_KEY") == "secret"
    assert os.environ.get("GIGASEARCH_VERIFY_SSL") == "False"


def test_fusionbrain_settings_are_typed_and_projected(monkeypatch):
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_BASE_URL", "https://fusion-brain.local")
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_PIPELINE_ID", "pipeline-under-test")
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_CERT_PATH", "/certs/client.pem")
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_KEY_PATH", "/certs/client.key")
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_CA_PATH", "/certs/ca.pem")
    monkeypatch.setitem(os.environ, "FUSIONBRAIN_VERIFY_SSL", "false")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.FUSIONBRAIN_BASE_URL == "https://fusion-brain.local"
    assert loaded.FUSIONBRAIN_PIPELINE_ID == "pipeline-under-test"
    assert loaded.get("FUSIONBRAIN_CERT_PATH") == "/certs/client.pem"
    assert loaded.get("FUSIONBRAIN_KEY_PATH") == "/certs/client.key"
    assert loaded.get("FUSIONBRAIN_CA_PATH") == "/certs/ca.pem"
    assert loaded.FUSIONBRAIN_VERIFY_SSL is False
    assert AppSettings().FUSIONBRAIN_VERIFY_SSL is True

    apply_settings_to_env(loaded)
    assert os.environ.get("FUSIONBRAIN_CERT_PATH") == "/certs/client.pem"
    assert os.environ.get("FUSIONBRAIN_VERIFY_SSL") == "False"


def test_gr_settings_are_typed_and_projected(monkeypatch):
    monkeypatch.setitem(os.environ, "GR_ENABLED", "true")
    monkeypatch.setitem(os.environ, "GR_CHECK_ONLY_URL", "https://gr.local/check_only")
    monkeypatch.setitem(os.environ, "GR_AGENT_ID", "CI12345678")
    monkeypatch.setitem(os.environ, "GR_VERIFY_SSL", "false")
    # An empty timeout must fall back to the documented default, not to zero.
    monkeypatch.setitem(os.environ, "GR_TIMEOUT_SEC", "")
    monkeypatch.setitem(os.environ, "GR_CONNECT_TIMEOUT_SEC", "5")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.GR_ENABLED is True
    assert loaded.GR_CHECK_ONLY_URL == "https://gr.local/check_only"
    assert loaded.GR_AGENT_ID == "CI12345678"
    assert loaded.GR_VERIFY_SSL is False
    assert loaded.GR_TIMEOUT_SEC == 30.0
    assert loaded.GR_CONNECT_TIMEOUT_SEC == 5.0
    assert loaded.GR_MODEL == "GigaChat"
    assert AppSettings().GR_ENABLED is False
    assert AppSettings().GR_CHECK_ONLY_URL == ""
    assert AppSettings().GR_CONNECT_TIMEOUT_SEC == 30.0

    apply_settings_to_env(loaded)
    assert os.environ.get("GR_CHECK_ONLY_URL") == "https://gr.local/check_only"
    assert os.environ.get("GR_VERIFY_SSL") == "False"
    assert os.environ.get("GR_CONNECT_TIMEOUT_SEC") == "5.0"


@pytest.mark.parametrize("field", ["GR_TIMEOUT_SEC", "GR_CONNECT_TIMEOUT_SEC"])
def test_gr_timeouts_reject_a_non_positive_value(field):
    with pytest.raises(ValidationError):
        AppSettings.model_validate({field: 0})


def test_dlp_settings_are_typed_and_projected(monkeypatch):
    monkeypatch.setitem(os.environ, "DLP_BASE_URL", "http://dlp.local:3010")
    monkeypatch.setitem(os.environ, "DLP_SENDER_EMAIL", "tuz@sberbank.ru")
    monkeypatch.setitem(os.environ, "DLP_TIMEOUT_SEC", "45")
    reset_memory_settings_for_tests()

    loaded = load_settings()

    assert loaded.DLP_BASE_URL == "http://dlp.local:3010"
    assert loaded.DLP_SENDER_EMAIL == "tuz@sberbank.ru"
    assert loaded.DLP_TIMEOUT_SEC == 45.0
    assert loaded.DLP_MAPPING_SCHEMA == "orion"
    assert loaded.DLP_RECIPIENT_EMAIL == "IHAPI-Internal"
    assert loaded.DLP_SERVER_IP == "1.1.1.1"
    assert loaded.DLP_CLIENT_IP == "1.1.1.1"
    assert loaded.DLP_SUBJECT == "Поиск в интернете"
    assert loaded.DLP_CONTENT_TYPE == "application/octet-stream"
    assert loaded.DLP_ALLOW == "204"
    assert AppSettings().DLP_BASE_URL == ""
    assert AppSettings().DLP_SENDER_EMAIL == ""

    apply_settings_to_env(loaded)
    assert os.environ.get("DLP_BASE_URL") == "http://dlp.local:3010"
    assert os.environ.get("DLP_SENDER_EMAIL") == "tuz@sberbank.ru"
    assert os.environ.get("DLP_MAPPING_SCHEMA") == "orion"


def test_development_allows_github_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_dev")
    reset_memory_settings_for_tests()
    assert load_settings().get("GITHUB_TOKEN") == "ghp_dev"
    assert not is_production()


def test_memory_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "development")
    reset_memory_settings_for_tests()

    payload = AppSettings(MCP_ENABLED=True, MCP_TOOL_TIMEOUT_SEC=45)
    save_settings(payload)
    loaded = load_settings()
    assert loaded.MCP_ENABLED is True
    assert loaded.MCP_TOOL_TIMEOUT_SEC == 45


def test_subprocess_env_scrubs_removed_model_code_keys(monkeypatch):
    """Stale OUROBOROS_MODEL_CODE / USE_LOCAL_CODE from a deployment env must not
    be inherited by child processes after the code model lane removal."""
    from ouroboros.storage.settings.store import build_subprocess_env

    monkeypatch.setenv("OUROBOROS_ENV", "development")
    reset_memory_settings_for_tests()

    base = {
        "PATH": "/usr/bin",
        "OUROBOROS_MODEL_CODE": "gigachat-b2bank::GigaChat-Pro",
        "USE_LOCAL_CODE": "true",
    }
    env = build_subprocess_env(AppSettings(), base=base)

    assert "OUROBOROS_MODEL_CODE" not in env
    assert "USE_LOCAL_CODE" not in env
    assert env["PATH"] == "/usr/bin"


@parametrize_from_dict(
    {
        "pulse and heimdall": {
            "removed_keys": {
                "PULSE_PEOPLE_BASE_URL",
                "HEIMDALL_BASE_URL",
                "PULSE_PEOPLE_EMPLOYEE_ACTUAL_PATH",
                "PULSE_PEOPLE_EMPLOYEE_COMPETENCE_PATH",
                "PULSE_PEOPLE_MCP_QUERY_PATH",
                "PULSE_PEOPLE_MCP_DOCS_PATH",
                "PULSE_PEOPLE_MCP_MODELS_PATH",
                "PULSE_PEOPLE_TOKEN",
                "HEIMDALL_TOKEN",
                "PULSE_PEOPLE_TIMEOUT_SEC",
                "PULSE_PEOPLE_TOTAL_TIMEOUT_SEC",
                "PULSE_PEOPLE_API_LOG_REQUESTS",
                "PULSE_PEOPLE_MAX_PARALLEL_REQUESTS",
                "PULSE_PEOPLE_LEGACY_OPTION",
                "HEIMDALL_LEGACY_OPTION",
            }
        },
        "navigator and assignment": {
            "removed_keys": {
                "NAVIGATOR_BI_BASE_URL",
                "NAVIGATOR_BI_SBER_PDI",
                "NAVIGATOR_BI_LEGACY_OPTION",
                "ASSISTANT_ASSIGNMENT_SMTP_HOST",
                "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD",
                "ASSISTANT_ASSIGNMENT_LEGACY_OPTION",
                "ASSISTANT_DIALOGS_BOT_TOKEN",
                "ASSISTANT_DIALOGS_BOT_GROUP_ID",
            }
        },
    }
)
def test_removed_settings_are_scrubbed_from_child_environments(monkeypatch, removed_keys):
    assert removed_keys.isdisjoint(AppSettings.model_fields)
    stale_env = dict.fromkeys(removed_keys, "stale")

    projected = build_subprocess_env(AppSettings(), base={"PATH": "/usr/bin", **stale_env})
    assert removed_keys.isdisjoint(projected)
    assert projected["PATH"] == "/usr/bin"

    for key, value in stale_env.items():
        monkeypatch.setenv(key, value)
    apply_settings_to_env(AppSettings())
    assert removed_keys.isdisjoint(os.environ)


def test_removed_settings_are_dropped_before_model_extras_and_persistence():
    settings = AppSettings.model_validate(
        {
            "HEIMDALL_TOKEN": "retired-heimdall",
            "PULSE_PEOPLE_BASE_URL": "https://retired.example.test",
            "HEIMDALL_LEGACY_OPTION": "retired-heimdall-extra",
            "PULSE_PEOPLE_LEGACY_OPTION": "retired-pulse-extra",
            "NAVIGATOR_BI_SBER_PDI": "retired-navigator-secret",
            "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD": "retired-smtp-secret",
            "CUSTOM_EXTENSION_TOKEN": "extension-secret",
        }
    )

    assert "HEIMDALL_TOKEN" not in settings
    assert "PULSE_PEOPLE_BASE_URL" not in settings
    assert "HEIMDALL_LEGACY_OPTION" not in settings
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in settings
    assert "NAVIGATOR_BI_SBER_PDI" not in settings
    assert "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD" not in settings
    assert settings.extra_fields() == {"CUSTOM_EXTENSION_TOKEN": "extension-secret"}
    persisted = settings.to_persist_dict()
    assert "HEIMDALL_TOKEN" not in persisted
    assert "PULSE_PEOPLE_BASE_URL" not in persisted
    assert "HEIMDALL_LEGACY_OPTION" not in persisted
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in persisted
    assert "NAVIGATOR_BI_SBER_PDI" not in persisted
    assert "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD" not in persisted
    assert persisted["CUSTOM_EXTENSION_TOKEN"] == "extension-secret"


def test_every_secret_field_survives_a_persist_round_trip():
    """A masked secret re-validated from the persist dict is `**********`, not the value."""
    secret_fields = [name for name, field in AppSettings.model_fields.items() if field.annotation is SecretStr]
    settings = AppSettings.model_validate({name: f"value-of-{name}" for name in secret_fields})

    restored = AppSettings.model_validate(settings.to_persist_dict())

    assert {"OUROBOROS_CHAT_ENCRYPTION_KEY", "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY"} <= set(secret_fields)
    assert {name: getattr(restored, name).get_secret_value() for name in secret_fields} == {
        name: f"value-of-{name}" for name in secret_fields
    }


def test_initial_extension_schema_budget_coerces_env_string(monkeypatch):
    monkeypatch.setenv("OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET", "16000")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET == 16000
    monkeypatch.delenv("OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET == 8000


def test_initial_extension_schema_budget_rejects_non_positive():
    with pytest.raises(ValueError, match="OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET"):
        AppSettings(OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET=0)
    with pytest.raises(ValueError, match="OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET"):
        AppSettings(OUROBOROS_INITIAL_EXTENSION_SCHEMA_BUDGET=-1)


def test_memory_distill_max_tokens_coerces_env_string(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MEMORY_DISTILL_MAX_TOKENS", "2048")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_MEMORY_DISTILL_MAX_TOKENS == 2048
    monkeypatch.delenv("OUROBOROS_MEMORY_DISTILL_MAX_TOKENS")
    reset_memory_settings_for_tests()
    assert load_settings().OUROBOROS_MEMORY_DISTILL_MAX_TOKENS == 32_768


def test_memory_distill_max_tokens_rejects_non_positive():
    with pytest.raises(ValueError, match="OUROBOROS_MEMORY_DISTILL_MAX_TOKENS"):
        AppSettings(OUROBOROS_MEMORY_DISTILL_MAX_TOKENS=0)
    with pytest.raises(ValueError, match="OUROBOROS_MEMORY_DISTILL_MAX_TOKENS"):
        AppSettings(OUROBOROS_MEMORY_DISTILL_MAX_TOKENS=-1)


def test_aef_tool_trace_payload_flag_coerces_env_string(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("AEF_TOOL_TRACE_PAYLOAD_ENABLED", "true")
    reset_memory_settings_for_tests()
    assert load_settings().AEF_TOOL_TRACE_PAYLOAD_ENABLED is True
    monkeypatch.setenv("AEF_TOOL_TRACE_PAYLOAD_ENABLED", "")
    reset_memory_settings_for_tests()
    assert load_settings().AEF_TOOL_TRACE_PAYLOAD_ENABLED is False
