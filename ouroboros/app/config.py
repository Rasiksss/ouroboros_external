"""
Ouroboros — Shared configuration (single source of truth).

Thin facade over ``ouroboros.storage.settings`` for paths, typed settings, and helpers.
"""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros.storage.settings import paths as _path_constants
from ouroboros.storage.settings.bootstrap import bootstrap_config, get_config
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.storage.settings.store import build_subprocess_env

HOME = _path_constants.HOME
RESTART_EXIT_CODE = _path_constants.RESTART_EXIT_CODE
AGENT_SERVER_PORT = _path_constants.AGENT_SERVER_PORT

APP_ROOT: pathlib.Path
REPO_DIR: pathlib.Path
DATA_DIR: pathlib.Path
SETTINGS_PATH: pathlib.Path
PID_FILE: pathlib.Path
PORT_FILE: pathlib.Path
_SETTINGS_LOCK: pathlib.Path
from ouroboros.storage.blob import (
    get_storage_provider,
    reset_storage_provider_for_tests,
    validate_production_storage,
)
from ouroboros.storage.settings.profile import (
    get_deploy_env,
    is_development,
    is_production,
    is_server_profile,
)
from ouroboros.storage.settings.schema import (
    AppSettings,
    get_defaults,
    get_defaults_dict,
)
from ouroboros.storage.settings.store import (
    apply_settings_to_env,
    load_settings,
    save_settings,
)
from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, user_settings

# Deprecated dict alias for tests and transitional imports.
SETTINGS_DEFAULTS = get_defaults_dict()

__all__ = [
    "apply_settings_to_env",
    "bootstrap_config",
    "build_subprocess_env",
    "get_config",
    "get_deploy_env",
    "get_storage_provider",
    "is_development",
    "is_production",
    "is_server_profile",
    "load_settings",
    "reset_storage_provider_for_tests",
    "save_settings",
    "validate_production_storage",
]

_VALID_EFFORTS = ("none", "low", "medium", "high")
_DIRECT_PROVIDER_REVIEW_RUNS = 3


def _refresh_path_aliases() -> None:
    global APP_ROOT, REPO_DIR, DATA_DIR, SETTINGS_PATH, PID_FILE, PORT_FILE, _SETTINGS_LOCK
    paths = load_path_settings()
    APP_ROOT = paths.app_root
    REPO_DIR = paths.repo_dir
    DATA_DIR = paths.data_dir
    SETTINGS_PATH = paths.settings_path
    PID_FILE = paths.pid_file
    PORT_FILE = paths.port_file
    _SETTINGS_LOCK = pathlib.Path(str(SETTINGS_PATH) + ".lock")


def get_light_model() -> str:
    light = user_settings.OUROBOROS_MODEL_LIGHT(SETTINGS_OWNER_ID).strip()
    return light or get_defaults().OUROBOROS_MODEL_LIGHT


def get_main_model() -> str:
    main = user_settings.OUROBOROS_MODEL(SETTINGS_OWNER_ID).strip()
    return main or get_defaults().OUROBOROS_MODEL


def _parse_model_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _exclusive_direct_remote_provider_env() -> str:
    from ouroboros.llm.providers.gigachat_b2bank import gigachat_b2bank_credentials_ready

    settings = load_settings()
    if gigachat_b2bank_credentials_ready(settings):
        return "gigachat-b2bank"
    if str(settings.CLOUDRU_FOUNDATION_MODELS_API_KEY.get_secret_value() or "").strip():
        return "cloudru"
    return ""


def resolve_effort() -> str:
    """Effort level in force for the tasks this process runs."""
    effort = user_settings.OUROBOROS_EFFORT_TASK(SETTINGS_OWNER_ID)
    return effort if effort in _VALID_EFFORTS else get_defaults().OUROBOROS_EFFORT_TASK


def get_mcp_enabled() -> bool:
    return bool(load_settings().MCP_ENABLED)


def get_mcp_tool_timeout_sec() -> int:
    parsed = load_settings().MCP_TOOL_TIMEOUT_SEC
    return parsed if parsed > 0 else get_defaults().MCP_TOOL_TIMEOUT_SEC


def get_a2a_egress_enabled() -> bool:
    return bool(load_settings().A2A_EGRESS_ENABLED)


def get_a2a_tool_timeout_sec() -> int:
    parsed = load_settings().A2A_TOOL_TIMEOUT_SEC
    return parsed if parsed > 0 else get_defaults().A2A_TOOL_TIMEOUT_SEC


def _coerce_setting_value(key: str, value: Any) -> Any:
    """Backward-compatible coercion helper for tests."""
    validated = AppSettings.model_validate({key: value})
    return validated.get(key)


def init_config() -> None:
    """Bootstrap configuration from env and secret files; refresh path aliases."""
    bootstrap_config()
    _refresh_path_aliases()


# Reload path aliases when this module is reloaded (pytest isolation).
try:
    bootstrap_config()
    _refresh_path_aliases()
except Exception:
    _refresh_path_aliases()
