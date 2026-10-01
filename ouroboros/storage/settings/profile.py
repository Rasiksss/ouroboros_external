"""Deploy environment: production (default) or local development."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .schema import AppSettings

OUROBOROS_ENV_KEY = "OUROBOROS_ENV"
PRODUCTION = "production"
DEVELOPMENT = "development"

# Unused in production (no git, marketplace install, desktop self-mod paths).
PRODUCTION_IGNORED_SETTING_KEYS = frozenset(
    {
        "GITHUB_TOKEN",
        "GITHUB_REPO",
    }
)

PRODUCTION_IGNORED_ENV_PREFIXES = (
    "GITHUB_",
    "OPENROUTER_",
    "OPENAI_",
    "ANTHROPIC_",
    "OPENAI_COMPATIBLE_",
    "CLOUDRU_",
    "LOCAL_MODEL_",
    "USE_LOCAL_",
)

_deploy_env_snapshot: str | None = None


def resolve_deploy_env(environ: Mapping[str, str]) -> str:
    explicit = str(environ.get(OUROBOROS_ENV_KEY, "") or "").strip().lower()
    if explicit in (PRODUCTION, DEVELOPMENT):
        return explicit
    return PRODUCTION


def set_deploy_env_snapshot(value: str) -> None:
    global _deploy_env_snapshot
    _deploy_env_snapshot = value


def clear_deploy_env_snapshot() -> None:
    global _deploy_env_snapshot
    _deploy_env_snapshot = None


def get_deploy_env() -> str:
    if _deploy_env_snapshot is not None:
        return _deploy_env_snapshot
    return resolve_deploy_env(os.environ)


def is_production() -> bool:
    return get_deploy_env() == PRODUCTION


def is_development() -> bool:
    return get_deploy_env() == DEVELOPMENT


# Backward-compatible aliases used across the codebase.
def is_server_profile() -> bool:
    return is_production()


def apply_production_overlay(settings: AppSettings) -> AppSettings:
    # No production-forced setting overrides currently; kept as the hook point.
    return settings
