"""Pydantic AppSettings validation and env-only store."""

from __future__ import annotations

import json
import os

import pytest

from ouroboros.storage.settings.profile import apply_production_overlay, is_production
from ouroboros.storage.settings.schema import AppSettings, _coerce_mcp_servers
from ouroboros.storage.settings.store import (
    apply_settings_to_env,
    build_subprocess_env,
    load_settings,
    reset_memory_settings_for_tests,
    save_settings,
)


def test_mcp_servers_coerce_from_json_string():
    raw = json.dumps([{"id": "x", "url": "https://example.com/mcp", "enabled": True}])
    servers = _coerce_mcp_servers(raw)
    assert len(servers) == 1
    assert servers[0].id == "x"


def test_mcp_servers_drop_invalid_entries():
    servers = _coerce_mcp_servers([{"id": "ok"}, "bad", None])
    assert len(servers) == 1


def test_custom_secret_key_allowed():
    settings = AppSettings.model_validate({"GITHUB_TOKEN": "ghp_test"})
    assert settings.get("GITHUB_TOKEN") == "ghp_test"


def test_background_llm_timeout_is_typed():
    settings = AppSettings(OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC="45")
    assert settings.OUROBOROS_BACKGROUND_LLM_HTTP_READ_TIMEOUT_SEC == 45.0


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


def test_identity_self_edit_is_not_a_setting():
    """Identity is continuity: the switch is hardcoded off, so no config may claim it."""
    assert "OUROBOROS_IDENTITY_SELF_EDIT_ENABLED" not in AppSettings.model_fields


def test_browser_ingress_enabled_defaults_false_and_env_override(monkeypatch):
    reset_memory_settings_for_tests()
    assert load_settings().BROWSER_INGRESS_ENABLED is False
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "true")
    reset_memory_settings_for_tests()
    assert load_settings().BROWSER_INGRESS_ENABLED is True


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


@pytest.mark.parametrize("value", [0, 101])
def test_dialogs_history_recovery_limit_rejects_out_of_range(value):
    with pytest.raises(ValueError, match="DIALOGS_HISTORY_RECOVERY_LIMIT"):
        AppSettings(DIALOGS_HISTORY_RECOVERY_LIMIT=value)


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


def test_domain_assignment_and_navigator_settings_are_present():
    """Remaining domain tool settings must stay wired into AppSettings."""
    required_fields = {
        "ASSISTANT_ASSIGNMENT_SMTP_HOST",
        "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD",
        "ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC",
        "NAVIGATOR_BI_BASE_URL",
        "NAVIGATOR_BI_SBER_PDI",
    }

    assert required_fields <= set(AppSettings.model_fields)


@pytest.mark.parametrize(
    "values",
    [
        {
            "ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.example.test",
            "ASSISTANT_ASSIGNMENT_SMTP_USERNAME": "smtp-user",
            "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD": "smtp-secret",
            "ASSISTANT_ASSIGNMENT_MAIL_FROM": "assignments@example.test",
            "ASSISTANT_ASSIGNMENT_MAIL_TO": "recipient@example.test",
        },
        {
            "DIALOGS_GRPC_ENDPOINT": "dialogs.example.test:443",
            "ASSISTANT_DIALOGS_BOT_TOKEN": "dialogs-secret",
            "ASSISTANT_DIALOGS_BOT_GROUP_ID": "42",
        },
    ],
)
def test_assignment_delivery_accepts_each_complete_channel(values):
    AppSettings(**values)


@pytest.mark.parametrize(
    ("values", "missing"),
    [
        (
            {"ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.example.test"},
            "ASSISTANT_ASSIGNMENT_SMTP_USERNAME",
        ),
        (
            {"ASSISTANT_DIALOGS_BOT_TOKEN": "dialogs-secret"},
            "ASSISTANT_DIALOGS_BOT_GROUP_ID",
        ),
        (
            {
                "ASSISTANT_ASSIGNMENT_SMTP_HOST": "smtp.example.test",
                "ASSISTANT_ASSIGNMENT_SMTP_USERNAME": "smtp-user",
                "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD": "smtp-secret",
                "ASSISTANT_ASSIGNMENT_MAIL_FROM": "assignments@example.test",
                "ASSISTANT_ASSIGNMENT_MAIL_TO": " , ; ",
            },
            "ASSISTANT_ASSIGNMENT_MAIL_TO",
        ),
    ],
)
def test_assignment_delivery_rejects_partial_selected_channel(values, missing):
    with pytest.raises(ValueError, match=missing):
        AppSettings(**values)


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


def test_removed_pulse_settings_are_scrubbed_from_child_environments(monkeypatch):
    removed_keys = {
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
            "CUSTOM_EXTENSION_TOKEN": "extension-secret",
        }
    )

    assert "HEIMDALL_TOKEN" not in settings
    assert "PULSE_PEOPLE_BASE_URL" not in settings
    assert "HEIMDALL_LEGACY_OPTION" not in settings
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in settings
    assert settings.extra_fields() == {"CUSTOM_EXTENSION_TOKEN": "extension-secret"}
    persisted = settings.to_persist_dict()
    assert "HEIMDALL_TOKEN" not in persisted
    assert "PULSE_PEOPLE_BASE_URL" not in persisted
    assert "HEIMDALL_LEGACY_OPTION" not in persisted
    assert "PULSE_PEOPLE_LEGACY_OPTION" not in persisted
    assert persisted["CUSTOM_EXTENSION_TOKEN"] == "extension-secret"


def test_aef_tool_trace_payload_flag_coerces_env_string(monkeypatch):
    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("AEF_TOOL_TRACE_PAYLOAD_ENABLED", "true")
    reset_memory_settings_for_tests()
    assert load_settings().AEF_TOOL_TRACE_PAYLOAD_ENABLED is True
    monkeypatch.setenv("AEF_TOOL_TRACE_PAYLOAD_ENABLED", "")
    reset_memory_settings_for_tests()
    assert load_settings().AEF_TOOL_TRACE_PAYLOAD_ENABLED is False
