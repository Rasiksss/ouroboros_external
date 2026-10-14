"""Browser health and runtime state HTTP handlers."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from importlib.metadata import version
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from new_core.identity import OwnerScope
from ouroboros.storage.settings.profile import is_development
from ouroboros.storage.settings.store import load_settings
from transport.browser._helpers import (
    json_error,
    reject_unexpected_query,
    request_repo_dir,
    state_attr,
)
from transport.browser.contracts import BrowserHealthResponse, StateResponse
from transport.browser.identity import IdentityRefused, browser_identity

log = logging.getLogger(__name__)

# These numbers are the supervisor's, and asking it for them directly is what
# used to make this module import the core. The composition root puts a callable
# on ``app.state`` instead, so the knowledge stays on its side of the seam.
_EMPTY_RUNTIME_STATUS: dict[str, int] = {
    "workers_total": 0,
    "workers_busy": 0,
    "pending_count": 0,
    "running_count": 0,
    "spent_calls": 0,
}


def _runtime_version() -> str:
    try:
        return version("ouroboros")
    except Exception:
        return "1.0.0"


def _deploy_git_info(repo_dir: os.PathLike[str] | str) -> tuple[str, str]:
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
        return branch or "ouroboros", sha[:8]
    except Exception:
        return "ouroboros", ""


def _github_token_configured() -> bool:
    settings = load_settings()
    token = str(settings.get("GITHUB_TOKEN") or "").strip()
    return bool(token)


def _runtime_status(request: Request, owner_scope: OwnerScope) -> dict[str, Any]:
    """Queue depth, worker slots and spend, as reported by the composition root."""
    provider = state_attr(request, "runtime_status")
    if not callable(provider):
        return dict(_EMPTY_RUNTIME_STATUS)
    try:
        return {**_EMPTY_RUNTIME_STATUS, **(provider(owner_scope) or {})}
    except Exception:
        log.debug("Failed to read runtime status", exc_info=True)
        return dict(_EMPTY_RUNTIME_STATUS)


def _build_state_payload(request: Request, owner_scope: OwnerScope) -> StateResponse:
    status = _runtime_status(request, owner_scope)
    pending_count = int(status["pending_count"])
    running_count = int(status["running_count"])
    workers_total = int(status["workers_total"])
    workers_busy = int(status["workers_busy"])
    branch, sha = _deploy_git_info(request_repo_dir(request))
    supervisor_ready_event = state_attr(request, "supervisor_ready_event")
    get_supervisor_error = state_attr(request, "get_supervisor_error")
    app_start = float(state_attr(request, "app_start", time.time()) or time.time())
    return {
        "uptime": int(time.time() - app_start),
        "workers_total": workers_total,
        "workers_busy": workers_busy,
        "pending_count": pending_count,
        "running_count": running_count,
        "branch": branch,
        "sha": sha,
        "spent_calls": int(status["spent_calls"]),
        "supervisor_ready": bool(supervisor_ready_event.is_set()) if supervisor_ready_event else False,
        "supervisor_error": get_supervisor_error() if callable(get_supervisor_error) else None,
        "runtime_mode": "development" if is_development() else "headless",
        "skills_repo_configured": (request_repo_dir(request) / "skills").is_dir(),
        "github_token_configured": _github_token_configured(),
    }


async def api_health(request: Request) -> JSONResponse:
    query_error = reject_unexpected_query(request)
    if query_error is not None:
        return query_error
    runtime_version = _runtime_version()
    app_version = runtime_version
    payload: BrowserHealthResponse = {
        "status": "ok",
        "version": runtime_version,
        "runtime_version": runtime_version,
        "app_version": app_version,
    }
    return JSONResponse(payload)


async def api_state(request: Request) -> JSONResponse:
    """The runtime as the caller sees it: their queue and their spend.

    Resolved per request, fail-closed, like the handshake and the history route. A request
    naming no agent and no conversation resolves the configured owner as it always has; one
    that names either is refused on the same terms as the handshake. ``/api/health`` stays
    the unauthenticated probe surface.
    """
    query_error = reject_unexpected_query(request)
    if query_error is not None:
        return query_error
    try:
        owner_scope = await browser_identity.resolver.scope_for(request.headers)
    except IdentityRefused as refusal:
        return json_error(refusal.reason, status=refusal.status)
    try:
        payload = await asyncio.to_thread(_build_state_payload, request, owner_scope)
        return JSONResponse(payload)
    except Exception as exc:
        log.warning("Failed to build browser state payload: %s", exc)
        return json_error("failed to load runtime state", status=500)


__all__ = ["api_health", "api_state"]
