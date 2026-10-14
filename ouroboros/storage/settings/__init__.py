"""Typed configuration: env + in-memory only (no settings file)."""

from .bootstrap import RuntimeConfiguration, bootstrap_config, get_config, reset_config_for_tests
from .paths import PathSettings, load_path_settings
from .profile import (
    DEVELOPMENT,
    OUROBOROS_ENV_KEY,
    PRODUCTION,
    apply_production_overlay,
    get_deploy_env,
    is_development,
    is_production,
    is_server_profile,
)
from .schema import (
    AppSettings,
    McpServerEntry,
    get_defaults,
    get_defaults_dict,
)
from .store import (
    apply_settings_to_env,
    build_subprocess_env,
    load_settings,
    save_settings,
)

__all__ = [
    "DEVELOPMENT",
    "OUROBOROS_ENV_KEY",
    "PRODUCTION",
    "AppSettings",
    "McpServerEntry",
    "PathSettings",
    "RuntimeConfiguration",
    "apply_production_overlay",
    "apply_settings_to_env",
    "bootstrap_config",
    "build_subprocess_env",
    "get_config",
    "get_defaults",
    "get_defaults_dict",
    "get_deploy_env",
    "is_development",
    "is_production",
    "is_server_profile",
    "load_path_settings",
    "load_settings",
    "reset_config_for_tests",
    "save_settings",
]
