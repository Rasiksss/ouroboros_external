"""Minimal Reflex/Dynatrace OneAgent lifecycle integration.

This module deliberately initializes infrastructure monitoring only. It must
not pass chat text, LLM payloads, tool inputs, settings, or custom attributes to
the OneAgent SDK.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_ENABLED_KEYS = ("AEF_REFLEX_ENABLED", "DYNATRACE_ENABLED", "ENABLE_REFLEX")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_LOCK = threading.Lock()
_INITIALIZED = False
_ONEAGENT: Any | None = None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _first_flag_value() -> Any:
    try:
        from ouroboros.app.config import load_settings

        settings = load_settings()
        for key in _ENABLED_KEYS:
            value = settings.get(key, "")
            if str(value or "").strip():
                return value
    except Exception:
        log.debug("Could not read Reflex monitoring setting", exc_info=True)
    return ""


def _diagnostic_callback(message: str) -> None:
    log.debug("OneAgent SDK diagnostic: %s", message)


def _log_sdk_state(sdk: Any) -> None:
    log.debug(
        "OneAgent SDK state agent_state=%s agent_found=%s agent_is_compatible=%s agent_version=%s",
        getattr(sdk, "agent_state", ""),
        getattr(sdk, "agent_found", ""),
        getattr(sdk, "agent_is_compatible", ""),
        getattr(sdk, "agent_version_string", ""),
    )


def initialize_dynatrace_monitoring() -> bool:
    """Initialize OneAgent when Reflex monitoring is enabled."""
    global _INITIALIZED, _ONEAGENT

    if not _truthy(_first_flag_value()):
        return False

    with _LOCK:
        if _INITIALIZED:
            return True
        try:
            oneagent = importlib.import_module("oneagent")
        except Exception as exc:
            log.warning("OneAgent SDK import failed; Reflex monitoring is disabled: %s", exc)
            return False

        try:
            init_result = oneagent.initialize()
            if init_result is None:
                log.error("OneAgent SDK initialization returned no result")
                return False
            init_error = getattr(init_result, "error", None)
            if init_error is not None:
                log.error("OneAgent SDK initialization failed: %s", init_error)
                return False

            sdk = oneagent.get_sdk()
            sdk.set_diagnostic_callback(_diagnostic_callback)
            _log_sdk_state(sdk)
        except Exception:
            log.warning("OneAgent SDK setup failed; Reflex monitoring is disabled", exc_info=True)
            return False

        _ONEAGENT = oneagent
        _INITIALIZED = True
        log.info("Reflex monitoring initialized via OneAgent SDK")
        return True


def shutdown_dynatrace_monitoring() -> None:
    """Shutdown OneAgent if this process initialized it."""
    global _INITIALIZED, _ONEAGENT

    with _LOCK:
        if not _INITIALIZED or _ONEAGENT is None:
            return
        oneagent = _ONEAGENT
        _INITIALIZED = False
        _ONEAGENT = None

    try:
        shutdown_error = oneagent.shutdown()
        if shutdown_error:
            log.warning("OneAgent SDK shutdown reported an error: %s", shutdown_error)
    except Exception:
        log.warning("OneAgent SDK shutdown failed", exc_info=True)


def reset_dynatrace_monitoring_for_tests() -> None:
    global _INITIALIZED, _ONEAGENT
    with _LOCK:
        _INITIALIZED = False
        _ONEAGENT = None


__all__ = [
    "initialize_dynatrace_monitoring",
    "reset_dynatrace_monitoring_for_tests",
    "shutdown_dynatrace_monitoring",
]
