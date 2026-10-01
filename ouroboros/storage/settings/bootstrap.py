"""Bootstrap-only configuration: env + secret files → in-process singleton."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env_files import SECRETS_DIR_ENV, env_key_allowed, load_secret_files
from .paths import PathSettings, _refresh_path_module_aliases, resolve_path_settings
from .profile import (
    PRODUCTION,
    PRODUCTION_IGNORED_SETTING_KEYS,
    apply_production_overlay,
    resolve_deploy_env,
    set_deploy_env_snapshot,
)
from .schema import AppSettings, get_defaults

_VALID_EFFORTS: frozenset[str] = frozenset({"none", "low", "medium", "high"})

_MEMORY_SETTINGS: AppSettings | None = None
_BASE_MERGED: dict[str, Any] | None = None
_config: RuntimeConfiguration | None = None


@dataclass(frozen=True)
class RuntimeConfiguration:
    settings: AppSettings
    paths: PathSettings
    deploy_env: str


def get_memory_overlay() -> AppSettings | None:
    return _MEMORY_SETTINGS


def _merge_env_and_secrets(
    environ: Mapping[str, str],
    *,
    deploy_env: str,
    secrets_dir: Path | None = None,
) -> dict[str, Any]:
    defaults = get_defaults()
    merged = defaults.to_persist_dict()
    production = deploy_env == PRODUCTION

    for key in defaults.known_field_names():
        if production and key in PRODUCTION_IGNORED_SETTING_KEYS:
            continue
        raw_env = environ.get(key)
        if raw_env is None or raw_env == "":
            continue
        merged[key] = raw_env

    for key, raw_env in environ.items():
        if not raw_env:
            continue
        if key in defaults.known_field_names():
            continue
        if merged.get(key) not in {None, ""}:
            continue
        if not env_key_allowed(key, deploy_env=deploy_env):
            continue
        merged[key] = raw_env

    for key, value in load_secret_files(
        secrets_dir=secrets_dir,
        environ=environ,
        deploy_env=deploy_env,
        poll=True,
    ).items():
        merged[key] = value

    return merged


def _apply_memory_overlay_dict(
    merged: dict[str, Any],
    *,
    deploy_env: str,
) -> dict[str, Any]:
    if _MEMORY_SETTINGS is None:
        return merged
    base = dict(merged)
    overlay = _MEMORY_SETTINGS.to_persist_dict()
    if deploy_env == PRODUCTION:
        for key in PRODUCTION_IGNORED_SETTING_KEYS:
            overlay.pop(key, None)
    base.update(overlay)
    return base


def _coerce_invalid_env_values(merged: dict[str, Any]) -> dict[str, Any]:
    """Drop invalid raw env strings so bootstrap matches runtime resolver fallbacks."""
    out = dict(merged)
    effort = out.get("OUROBOROS_EFFORT_TASK")
    if effort is not None and str(effort) not in _VALID_EFFORTS:
        out.pop("OUROBOROS_EFFORT_TASK", None)
    return out


def _finalize_settings(merged: dict[str, Any], *, deploy_env: str) -> AppSettings:
    settings = AppSettings.model_validate(_coerce_invalid_env_values(merged))
    settings = apply_production_overlay(settings)
    return settings


def _build_config(
    *,
    environ: Mapping[str, str],
    deploy_env: str,
    secrets_dir: Path | None = None,
    base_merged: dict[str, Any] | None = None,
) -> RuntimeConfiguration:
    merged_base = (
        base_merged
        if base_merged is not None
        else _merge_env_and_secrets(
            environ,
            deploy_env=deploy_env,
            secrets_dir=secrets_dir,
        )
    )
    merged = _apply_memory_overlay_dict(merged_base, deploy_env=deploy_env)
    settings = _finalize_settings(merged, deploy_env=deploy_env)
    paths = resolve_path_settings(merged)
    set_deploy_env_snapshot(deploy_env)
    return RuntimeConfiguration(settings=settings, paths=paths, deploy_env=deploy_env)


def bootstrap_config(
    *,
    environ: Mapping[str, str] | None = None,
    secrets_dir: Path | None = None,
    force: bool = False,
) -> RuntimeConfiguration:
    """Load config once from OS env and optional secret files (files override env)."""
    global _config, _BASE_MERGED

    if _config is not None and not force:
        return _config

    source = os.environ if environ is None else environ
    deploy_env = resolve_deploy_env(source)
    _BASE_MERGED = _merge_env_and_secrets(source, deploy_env=deploy_env, secrets_dir=secrets_dir)
    _config = _build_config(
        environ=source,
        deploy_env=deploy_env,
        secrets_dir=secrets_dir,
        base_merged=_BASE_MERGED,
    )
    _refresh_path_module_aliases()
    return _config


def rebuild_config_from_memory() -> RuntimeConfiguration:
    """Rebuild singleton after save_settings without re-reading env/files."""
    global _config
    if _BASE_MERGED is None:
        return bootstrap_config()
    deploy_env = resolve_deploy_env(os.environ)
    _config = _build_config(
        environ=os.environ,
        deploy_env=deploy_env,
        base_merged=_BASE_MERGED,
    )
    _refresh_path_module_aliases()
    return _config


def get_config() -> RuntimeConfiguration:
    if _config is None:
        return bootstrap_config()
    return _config


def reset_config_for_tests() -> None:
    global _config, _MEMORY_SETTINGS, _BASE_MERGED
    from .profile import clear_deploy_env_snapshot

    _config = None
    _MEMORY_SETTINGS = None
    _BASE_MERGED = None
    clear_deploy_env_snapshot()


def set_memory_overlay(settings: AppSettings | None) -> None:
    global _MEMORY_SETTINGS
    _MEMORY_SETTINGS = settings


def set_memory_overlay_for_tests(settings: AppSettings | None) -> None:
    set_memory_overlay(settings)


def resolve_secrets_dir_from_environ(environ: Mapping[str, str] | None = None) -> Path | None:
    source = os.environ if environ is None else environ
    raw = str(source.get(SECRETS_DIR_ENV, "") or "").strip()
    return Path(raw) if raw else None
