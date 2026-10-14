"""Load/save settings via bootstrap singleton (no settings file)."""

from __future__ import annotations

import logging
from typing import Any

from .bootstrap import (
    bootstrap_config,
    get_config,
    rebuild_config_from_memory,
    reset_config_for_tests,
    set_memory_overlay,
)
from .profile import PRODUCTION_IGNORED_SETTING_KEYS, is_production
from .retired import is_retired_setting_key
from .schema import AppSettings

log = logging.getLogger(__name__)


def reset_memory_settings_for_tests() -> None:
    reset_config_for_tests()


def load_settings_raw() -> AppSettings:
    return get_config().settings


def load_settings() -> AppSettings:
    return get_config().settings


def save_settings(
    settings: AppSettings | dict[str, Any],
    *,
    allow_elevation: bool = False,
) -> None:
    del allow_elevation
    if isinstance(settings, dict):
        settings = AppSettings.model_validate(settings)

    set_memory_overlay(settings)
    rebuild_config_from_memory()
    log.debug("Updated in-memory settings (no settings file)")


def _env_keys_for_subprocess() -> list[str]:
    keys = [
        "GIGACHAT_B2BANK_BASE_URL",
        "GIGACHAT_B2BANK_CERT_PATH",
        "GIGACHAT_B2BANK_KEY_PATH",
        "GIGACHAT_B2BANK_CA_PATH",
        "GIGACHAT_B2BANK_VERIFY_SSL",
        "GIGACHAT_B2BANK_CHAT_API_VERSION",
        "GIGACHAT_B2BANK_CLIENT_ID",
        "GIGACHAT_B2BANK_TEMPERATURE",
        "GIGACHAT_B2BANK_TOP_P",
        "GIGACHAT_B2BANK_MAX_TOKENS",
        "GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS",
        "GIGACHAT_PREVIEW_ENABLED",
        "GIGACHAT_PREVIEW_MODEL",
        "GIGACHAT_PREVIEW_PROBABILITY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
        "SELFHOSTED_LLM_BASE_URL",
        "SELFHOSTED_LLM_API_KEY",
        "SELFHOSTED_LLM_CERT_PATH",
        "SELFHOSTED_LLM_KEY_PATH",
        "SELFHOSTED_LLM_CA_PATH",
        "SELFHOSTED_LLM_VERIFY_SSL",
        "GIGASEARCH_BASE_URL",
        "GIGASEARCH_USER_ID",
        "GIGASEARCH_SURFACE_ID",
        "GIGASEARCH_API_KEY",
        "GIGASEARCH_TIMEOUT_SEC",
        "GIGASEARCH_CERT_PATH",
        "GIGASEARCH_KEY_PATH",
        "GIGASEARCH_CA_PATH",
        "GIGASEARCH_VERIFY_SSL",
        "FUSIONBRAIN_BASE_URL",
        "FUSIONBRAIN_PIPELINE_ID",
        "FUSIONBRAIN_CERT_PATH",
        "FUSIONBRAIN_KEY_PATH",
        "FUSIONBRAIN_CA_PATH",
        "FUSIONBRAIN_VERIFY_SSL",
        "DLP_BASE_URL",
        "DLP_MAPPING_SCHEMA",
        "DLP_SENDER_EMAIL",
        "DLP_RECIPIENT_EMAIL",
        "DLP_SERVER_IP",
        "DLP_CLIENT_IP",
        "DLP_SUBJECT",
        "DLP_CONTENT_TYPE",
        "DLP_ALLOW",
        "DLP_TIMEOUT_SEC",
        "GR_ENABLED",
        "GR_CHECK_ONLY_URL",
        "GR_AGENT_ID",
        "GR_MODEL",
        "GR_TIMEOUT_SEC",
        "GR_CONNECT_TIMEOUT_SEC",
        "GR_VERIFY_SSL",
        "DIALOGS_GRPC_ENDPOINT",
        "DIALOGS_BOT_TOKEN",
        "DIALOGS_GROUP_ID",
        "DIALOGS_USER_ID",
        "DIALOGS_SECURE",
        "DIALOGS_ROOT_CERTIFICATES",
        "DIALOGS_REPLY_ENABLED",
        "DIALOGS_UPDATE_PROGRESS_MESSAGE_ENABLED",
        "DIALOGS_TARGETED_GROUP_MESSAGES_ENABLED",
        "DIALOGS_HISTORY_RECOVERY_ENABLED",
        "DIALOGS_HISTORY_RECOVERY_LIMIT",
        "DIALOGS_FORWARDING_DISABLED",
        "DIALOGS_TYPING_ENABLED",
        "DIALOGS_PARTICIPANT_IDENTITY_ENABLED",
        "DIALOGS_BOT_NICK",
        "OUROBOROS_USER_FILE_TTL_HOURS",
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK",
        "OUROBOROS_MODEL_VISION",
        "OUROBOROS_TOOL_TIMEOUT_SEC",
        "OUROBOROS_EFFORT_TASK",
        "A2A_ENABLED",
        "A2A_PORT",
        "A2A_HOST",
        "A2A_AGENT_NAME",
        "A2A_AGENT_DESCRIPTION",
        "A2A_PUBLIC_URL",
        "A2A_MAX_CONCURRENT",
        "A2A_TASK_TTL_HOURS",
        "AEF_TRACING_ENABLED",
        "AEF_TRACING_KAFKA_TOPIC",
        "AEF_TRACING_SDK_LIST",
        "AEF_TRACING_SDK_CONTRACT_VERSION",
        "AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED",
        "AEF_TOOL_TRACE_PAYLOAD_ENABLED",
    ]
    if not is_production():
        keys.extend(
            [
                "MCP_ENABLED",
                "MCP_TOOL_TIMEOUT_SEC",
                "A2A_EGRESS_ENABLED",
                "A2A_TOOL_TIMEOUT_SEC",
            ]
        )
    return keys


def build_subprocess_env(
    settings: AppSettings | dict[str, Any] | None = None,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Project effective settings into a child-process env dict."""
    import os

    from .profile import apply_production_overlay

    if settings is None:
        effective = load_settings()
    elif isinstance(settings, dict):
        effective = apply_production_overlay(AppSettings.model_validate(settings))
    else:
        effective = apply_production_overlay(settings)

    env = dict(base if base is not None else os.environ)
    for key in _env_keys_for_subprocess():
        val = effective.get(key)
        if val is None or val == "":
            env.pop(key, None)
        else:
            env[key] = str(val)
    for key in tuple(env):
        if is_retired_setting_key(key):
            env.pop(key, None)
    if is_production():
        env["MCP_ENABLED"] = "False"
        for key in PRODUCTION_IGNORED_SETTING_KEYS:
            env.pop(key, None)
    return env


def apply_settings_to_env(settings: AppSettings | dict[str, Any]) -> None:
    """Write projected settings into os.environ (subprocess bridge only)."""
    import os

    projected = build_subprocess_env(settings)
    for key in _env_keys_for_subprocess():
        if key in projected:
            os.environ[key] = projected[key]
        else:
            os.environ.pop(key, None)
    for key in tuple(os.environ):
        if is_retired_setting_key(key):
            os.environ.pop(key, None)
    if is_production():
        for key in PRODUCTION_IGNORED_SETTING_KEYS:
            os.environ.pop(key, None)


__all__ = [
    "apply_settings_to_env",
    "bootstrap_config",
    "build_subprocess_env",
    "load_settings",
    "load_settings_raw",
    "reset_memory_settings_for_tests",
    "save_settings",
]
