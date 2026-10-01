"""Load secret values from Vault-mounted files (one file per env key)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from pathlib import Path

from .profile import (
    PRODUCTION,
    PRODUCTION_IGNORED_ENV_PREFIXES,
    PRODUCTION_IGNORED_SETTING_KEYS,
)
from .retired import is_retired_setting_key

log = logging.getLogger(__name__)

SECRETS_DIR_ENV = "OUROBOROS_SECRETS_DIR"
SECRETS_WAIT_SEC_ENV = "OUROBOROS_SECRETS_WAIT_SEC"
SECRETS_WAIT_INTERVAL_ENV = "OUROBOROS_SECRETS_WAIT_INTERVAL_SEC"
DEFAULT_SECRETS_WAIT_SEC = 30.0
DEFAULT_SECRETS_WAIT_INTERVAL_SEC = 0.5
_ENV_KEY_LOWER_RE = re.compile(r"^[a-z][a-z0-9_]{2,}$")

_ENV_OPTIONAL_PREFIXES = (
    "GIGACHAT_",
    "CLOUDRU_",
    "SELFHOSTED_",
    "DIALOGS_",
    "OUROBOROS_",
    "MCP_",
    "A2A_",
    "TOTAL_",
    "GITHUB_",
    "OPENAI_",
    "ANTHROPIC_",
    "AEF_",
    "TOOL_",
)


def _production_blocked_key(key: str, *, deploy_env: str | None = None) -> bool:
    from .profile import get_deploy_env

    if (deploy_env if deploy_env is not None else get_deploy_env()) != PRODUCTION:
        return False
    if key in PRODUCTION_IGNORED_SETTING_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in PRODUCTION_IGNORED_ENV_PREFIXES)


def secret_file_key_allowed(key: str, *, deploy_env: str | None = None) -> bool:
    """Load valid, non-retired secret filenames; production also blocks ignored keys/prefixes."""
    return not is_retired_setting_key(key) and not _production_blocked_key(key, deploy_env=deploy_env)


def env_key_allowed(key: str, *, deploy_env: str | None = None) -> bool:
    if is_retired_setting_key(key) or _production_blocked_key(key, deploy_env=deploy_env):
        return False
    return any(key.startswith(prefix) for prefix in _ENV_OPTIONAL_PREFIXES)


def _env_key_from_filename(name: str) -> str | None:
    """Map a secret filename to a canonical UPPER_SNAKE env key (case-insensitive)."""
    key_lower = name.lower()
    if not _ENV_KEY_LOWER_RE.match(key_lower):
        return None
    return key_lower.upper()


def _resolve_secrets_dir(
    secrets_dir: Path | None,
    environ: Mapping[str, str],
) -> Path | None:
    if secrets_dir is not None:
        return secrets_dir
    raw = str(environ.get(SECRETS_DIR_ENV, "") or "").strip()
    if not raw:
        return None
    return Path(raw)


def _parse_positive_float(environ: Mapping[str, str], key: str, default: float) -> float:
    raw = str(environ.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _scan_secret_files_once(
    resolved_dir: Path,
    *,
    deploy_env: str | None,
) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for entry in sorted(resolved_dir.iterdir()):
        if not entry.is_file():
            continue
        key = _env_key_from_filename(entry.name)
        if key is None:
            continue
        if key in loaded:
            continue
        if not secret_file_key_allowed(key, deploy_env=deploy_env):
            continue
        try:
            value = entry.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("Could not read secret file for %s: %s", key, exc)
            continue
        if not value:
            continue
        loaded[key] = value
    return loaded


def wait_for_secrets_dir(
    *,
    secrets_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Poll until ``OUROBOROS_SECRETS_DIR`` exists or the wait budget is exhausted."""
    load_secret_files(secrets_dir=secrets_dir, environ=environ, poll=True)


def load_secret_files(
    *,
    secrets_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    deploy_env: str | None = None,
    poll: bool = False,
) -> dict[str, str]:
    """Read ``{OUROBOROS_SECRETS_DIR}/{KEY}`` files into a plain dict (no os.environ writes).

    Filenames are case-insensitive (``dialogs_bot_token`` → ``DIALOGS_BOT_TOKEN``).

    When ``poll`` is true, keep scanning until the wait budget expires or the file set
    is stable for several consecutive polls (Vault sidecars often mount files one by one).
    """
    import os

    source = os.environ if environ is None else environ
    resolved_dir = _resolve_secrets_dir(secrets_dir, source)
    if resolved_dir is None:
        return {}

    if not poll:
        if not resolved_dir.is_dir():
            log.debug("Secret files directory does not exist: %s", resolved_dir)
            return {}
        loaded = _scan_secret_files_once(resolved_dir, deploy_env=deploy_env)
        for key in loaded:
            log.debug("Loaded %s from secret file", key)
        return loaded

    wait_sec = _parse_positive_float(source, SECRETS_WAIT_SEC_ENV, DEFAULT_SECRETS_WAIT_SEC)
    if wait_sec <= 0:
        if not resolved_dir.is_dir():
            log.debug("Secret files directory does not exist: %s", resolved_dir)
            return {}
        immediate = _scan_secret_files_once(resolved_dir, deploy_env=deploy_env)
        for key in immediate:
            log.debug("Loaded %s from secret file", key)
        return immediate

    interval = _parse_positive_float(source, SECRETS_WAIT_INTERVAL_ENV, DEFAULT_SECRETS_WAIT_INTERVAL_SEC)
    if interval <= 0:
        interval = DEFAULT_SECRETS_WAIT_INTERVAL_SEC

    deadline = time.monotonic() + wait_sec
    polled: dict[str, str] = {}
    logged_dir_ready = False

    while time.monotonic() < deadline:
        if resolved_dir.is_dir():
            if not logged_dir_ready:
                log.info("Polling secret files directory: %s (up to %.0fs)", resolved_dir, wait_sec)
                logged_dir_ready = True
            batch = _scan_secret_files_once(resolved_dir, deploy_env=deploy_env)
            for key, value in batch.items():
                if polled.get(key) != value:
                    log.debug("Loaded %s from secret file", key)
            polled = batch
        else:
            log.info("Waiting for secret files directory: %s", resolved_dir)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if not resolved_dir.is_dir():
        log.warning(
            "Secret files directory not available after %.1fs: %s; continuing bootstrap without secret files",
            wait_sec,
            resolved_dir,
        )
    elif polled:
        log.info("Loaded %d secret file(s) from %s", len(polled), resolved_dir)

    return polled


def bootstrap_env_from_secret_files(
    *,
    secrets_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    force: bool = False,
) -> int:
    """Deprecated: load secret files into os.environ (tests only). Prefer bootstrap_config()."""
    del force
    import os
    from collections.abc import MutableMapping

    if environ is None:
        target: MutableMapping[str, str] = os.environ
    elif isinstance(environ, MutableMapping):
        target = environ
    else:
        target = dict(environ)
    secrets = load_secret_files(secrets_dir=secrets_dir, environ=target)
    count = 0
    for key, value in secrets.items():
        if not str(target.get(key, "") or "").strip():
            target[key] = value
            count += 1
    return count


def reset_env_files_bootstrap_for_tests() -> None:
    """No-op kept for test compatibility."""
