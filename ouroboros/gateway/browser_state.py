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

from ouroboros.app.config import is_development, load_settings
from ouroboros.gateway._helpers import (
    json_error,
    reject_unexpected_query,
    request_drive_root,
    request_repo_dir,
    state_attr,
)
from ouroboros.gateway.contracts import BrowserHealthResponse, StateResponse
from supervisor.queue import get_task_queue
from supervisor.runtime_context import try_get_runtime
from supervisor.state import get_state_repository

log = logging.getLogger(__name__)


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


def _queue_counts(request: Request) -> tuple[int, int]:
    try:
        task_queue = get_task_queue(request_drive_root(request))
        pending = len(task_queue.list_pending_tasks())
        running = len(task_queue.list_running_tasks())
        return pending, running
    except Exception:
        log.debug("Failed to read supervisor queue counts", exc_info=True)
        return 0, 0


def _worker_counts(request: Request) -> tuple[int, int]:
    try:
        runtime = try_get_runtime()
        if runtime is None:
            return 0, 0
        slots = runtime.executor.all_slots()
        total = len(slots)
        busy = sum(1 for slot in slots if slot.busy_task_id)
        return total, busy
    except Exception:
        log.debug("Failed to read worker counts", exc_info=True)
        return 0, 0


def _load_owner_state(request: Request) -> Any:
    return get_state_repository(request_drive_root(request)).load()


def _build_state_payload(request: Request) -> StateResponse:
    state = _load_owner_state(request)
    pending_count, running_count = _queue_counts(request)
    workers_total, workers_busy = _worker_counts(request)
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
        "spent_calls": int(getattr(state, "spent_calls", 0) or 0),
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
    query_error = reject_unexpected_query(request)
    if query_error is not None:
        return query_error
    try:
        payload = await asyncio.to_thread(_build_state_payload, request)
        return JSONResponse(payload)
    except Exception as exc:
        log.warning("Failed to build browser state payload: %s", exc)
        return json_error("failed to load runtime state", status=500)


__all__ = ["api_health", "api_state"]
