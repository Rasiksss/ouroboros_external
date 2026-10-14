"""Canary routing of a bounded share of requests to a preview model version (TN09).

The whole policy lives here and is driven by the GigaChat B2Bank adapter: which request
goes to the preview version and when the share degrades. Nothing above the provider takes
part — the version that answered is reported through the usage record and reaches events by
the ordinary path.

Three deliberate choices. The version under test is named outright by
``GIGACHAT_PREVIEW_MODEL`` instead of being derived from the installed name, so the check
can never route traffic to a build the platform did not publish. It shadows exactly one
model — the configured main one, never the fallback, vision or light lanes. And the share is
served by a counter rather than a dice roll: the requirement caps it at 5% strictly, and
random sampling overshoots that cap on small volumes.

**Coupling worth knowing.** The draw is per request, so two rounds of one dialogue may be
answered by different versions. That is safe only because the runtime does not replay
GigaChat's tool-state id: ``new_core/loop/nodes.py`` does not carry ``tools_state_id`` into
the transcript, so there is no provider round state to cross between versions. Restoring
that replay means revisiting this module.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from ouroboros.llm.models import parse_model_ref

log = logging.getLogger(__name__)

_MAX_FAILURES = 2


@dataclass
class _CanaryState:
    """Process-wide counters. One process, one task lane — see supervisor/workers.py."""

    considered: int = 0
    failures: int = 0
    misconfigured_reported: bool = False


_LOCK = threading.Lock()
_STATE = _CanaryState()


def reset_preview_canary_for_tests() -> None:
    global _STATE
    with _LOCK:
        _STATE = _CanaryState()


def _settings() -> Any:
    # Deferred: the adapter package is imported while settings are still initialising.
    from ouroboros.storage.settings.store import load_settings

    return load_settings()


def _model_id(configured: Any) -> str:
    """Model id of a configured model reference, or '' when it is unusable."""
    text = str(configured or "").strip()
    if not text:
        return ""
    try:
        return parse_model_ref(text).model_id.strip()
    except ValueError:
        return ""


def _report_misconfigured(setting: str, value: Any) -> None:
    """Say once why the check is doing nothing; a silent no-op reads as a healthy 0%."""
    with _LOCK:
        if _STATE.misconfigured_reported:
            return
        _STATE.misconfigured_reported = True
    log.warning(
        "Preview canary: enabled but idle — %s=%r is not a usable model reference",
        setting,
        str(value or ""),
    )


def _effective_probability(configured: float, failures: int) -> float:
    if failures >= _MAX_FAILURES:
        return 0.0
    return configured / (2**failures)


def select_request_model(model: str, *, bounded: bool = False) -> str:
    """Pick the version this one request runs on.

    ``bounded`` marks a caller that set a deadline — it has no room for the fallback
    attempt, so it never takes part. The returned name is always stripped, so a caller may
    compare it against the model it passed in.
    """
    installed = str(model or "").strip()
    settings = _settings()
    if not settings.GIGACHAT_PREVIEW_ENABLED or not installed:
        return installed

    preview = _model_id(settings.GIGACHAT_PREVIEW_MODEL)
    if not preview:
        _report_misconfigured("GIGACHAT_PREVIEW_MODEL", settings.GIGACHAT_PREVIEW_MODEL)
        return installed
    if installed == preview:
        return installed
    # Exactly one lane is shadowed. Fallback, vision and light keep their own model, so a
    # provider incident is never handled by the version under test.
    if bounded or installed != _model_id(settings.OUROBOROS_MODEL):
        return installed

    with _LOCK:
        share = _effective_probability(float(settings.GIGACHAT_PREVIEW_PROBABILITY), _STATE.failures)
        if share <= 0:
            return installed
        seen = _STATE.considered
        _STATE.considered = seen + 1
        # Route when this request crosses the next 1/share boundary.
        selected = int((seen + 1) * share) > int(seen * share)

    if not selected:
        return installed
    log.info(
        "Preview canary: routing request to preview version model=%s share=%.4f considered=%s",
        preview,
        share,
        seen + 1,
    )
    return preview


def record_preview_failure(model: str, *, request_id: str, reason: str) -> None:
    """Journal a failed preview call and degrade the share."""
    with _LOCK:
        _STATE.failures += 1
        failures = _STATE.failures
        # Restart the schedule, or credit accrued at the old rate delays the cut.
        _STATE.considered = 0
    outcome = "check disabled until restart" if failures >= _MAX_FAILURES else "share halved"
    log.warning(
        "Preview canary: preview version failed model=%s request_id=%s reason=%s failures=%s action=%s",
        model,
        request_id,
        reason,
        failures,
        outcome,
    )


__all__ = [
    "record_preview_failure",
    "reset_preview_canary_for_tests",
    "select_request_model",
]
