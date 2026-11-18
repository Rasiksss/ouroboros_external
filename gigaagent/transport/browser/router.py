"""Optional browser route collection."""

from __future__ import annotations

import asyncio
import logging
import pathlib

from starlette import status
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, WebSocketRoute

from new_core.identity import OwnerScope
from ouroboros.observability import aef_tracing
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import job_detail, job_summary
from transport.browser._helpers import TracedRoute, state_attr
from transport.browser.chat import (
    api_chat_file_download,
    api_chat_history,
    api_chat_upload,
    api_chat_upload_delete,
)
from transport.browser.confirmations import api_confirmation_decision
from transport.browser.contracts import JobResponse, JobsResponse
from transport.browser.files import (
    api_file_content,
    api_file_delete,
    api_file_entries,
    api_file_get,
    api_file_messages,
    api_file_patch,
    api_file_upload,
    api_folder_create,
    api_folder_delete,
    api_folder_patch,
)
from transport.browser.identity import IdentityRefused, browser_identity
from transport.browser.schedules import api_schedule_delete, api_schedules
from transport.browser.state import api_health, api_state
from transport.browser.ws import ws_endpoint
from transport.interfaces import InboundEnvelope, ITaskStopNotifier

log = logging.getLogger(__name__)


def collect_routes(
    *,
    data_dir: pathlib.Path,
    repo_dir: pathlib.Path,
) -> list[BaseRoute]:
    del data_dir, repo_dir  # reserved for chat/state handlers

    routes: list[BaseRoute] = [
        TracedRoute("/api/health", endpoint=api_health, methods=["GET"]),
        TracedRoute("/api/state", endpoint=api_state, methods=["GET"]),
        TracedRoute("/api/chat/history", endpoint=api_chat_history, methods=["GET"]),
        TracedRoute("/api/chat/schedules", endpoint=api_schedules, methods=["GET"]),
        TracedRoute("/api/chat/schedules/{schedule_id}", endpoint=api_schedule_delete, methods=["DELETE"]),
        TracedRoute("/api/chat/files/{path:path}", endpoint=api_chat_file_download, methods=["GET"]),
        TracedRoute("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
        TracedRoute("/api/chat/upload", endpoint=api_chat_upload_delete, methods=["DELETE"]),
        TracedRoute("/api/files", endpoint=api_file_upload, methods=["POST"]),
        TracedRoute("/api/files/entries", endpoint=api_file_entries, methods=["GET"]),
        TracedRoute("/api/files/{id}/messages", endpoint=api_file_messages, methods=["GET"]),
        TracedRoute("/api/files/{id}/content", endpoint=api_file_content, methods=["GET"]),
        TracedRoute("/api/files/{id}", endpoint=api_file_get, methods=["GET"]),
        TracedRoute("/api/files/{id}", endpoint=api_file_patch, methods=["PATCH"]),
        TracedRoute("/api/files/{id}", endpoint=api_file_delete, methods=["DELETE"]),
        TracedRoute("/api/folders", endpoint=api_folder_create, methods=["POST"]),
        TracedRoute("/api/folders/{id}", endpoint=api_folder_patch, methods=["PATCH"]),
        TracedRoute("/api/folders/{id}", endpoint=api_folder_delete, methods=["DELETE"]),
        TracedRoute(
            "/api/confirmations/{confirmation_id}",
            endpoint=api_confirmation_decision,
            methods=["POST"],
        ),
    ]
    routes.extend(
        [
            TracedRoute("/api/jobs", endpoint=api_jobs, methods=["GET"]),
            TracedRoute("/api/jobs/{job_id}", endpoint=api_job, methods=["GET"]),
            TracedRoute("/api/jobs/{job_id}/stop", endpoint=api_job_stop, methods=["POST"]),
        ]
    )
    routes.append(WebSocketRoute("/ws", endpoint=ws_endpoint))
    return routes


async def _scope(request: Request) -> OwnerScope | None:
    try:
        return await browser_identity.resolver.scope_for(request.headers)
    except IdentityRefused:
        return None


async def api_jobs(request: Request) -> JSONResponse:
    scope = await _scope(request)
    if scope is None:
        return JSONResponse({"error": "identity refused"}, status_code=status.HTTP_403_FORBIDDEN)
    rows = await asyncio.to_thread(
        runtime_persistence.current.tasks_repo.list_current_jobs,
        scope=scope,
        limit=101,
        active_only=True,
    )
    payload: JobsResponse = {
        "jobs": [job_summary(row) for row in rows[:100]],
        "truncated": len(rows) > 100,
        "detached_enabled": request.app.state.detached_enabled,
    }
    return JSONResponse(payload)


async def api_job(request: Request) -> JSONResponse:
    scope = await _scope(request)
    if scope is None:
        return JSONResponse({"error": "job not found"}, status_code=status.HTTP_403_FORBIDDEN)
    record = await asyncio.to_thread(
        runtime_persistence.current.tasks_repo.get_current_job, request.path_params["job_id"], scope=scope
    )
    if record is None:
        return JSONResponse({"error": "job not found"}, status_code=status.HTTP_404_NOT_FOUND)
    payload: JobResponse = {"job": job_detail(record)}
    return JSONResponse(payload)


async def api_job_stop(request: Request) -> JSONResponse:
    notify_chat = request.query_params.get("notify_chat", "true").lower()
    if notify_chat not in {"true", "false"}:
        return JSONResponse({"error": "notify_chat must be true or false"}, status_code=status.HTTP_400_BAD_REQUEST)
    scope = await _scope(request)
    if scope is None:
        return JSONResponse({"error": "job not found"}, status_code=status.HTTP_403_FORBIDDEN)
    notifier: ITaskStopNotifier = request.app.state.task_stop_notifier
    job_id = request.path_params["job_id"]
    rows = await asyncio.to_thread(runtime_persistence.current.tasks_repo.stop_jobs, scope=scope, job_id=job_id)
    if not rows:
        return JSONResponse({"error": "job not found"}, status_code=status.HTTP_404_NOT_FOUND)
    await asyncio.to_thread(notifier.notify, rows)
    gateway = state_attr(request, "message_gateway")
    if gateway is not None and notify_chat == "true":
        envelope = InboundEnvelope(
            scope=scope,
            user_id=scope.chat_id,
            text=f"/stop {job_id}",
            trace_id=aef_tracing.current_trace_id(),
            suppress_chat_log=True,
            suppress_acceptance_notice=True,
        )
        try:
            await asyncio.to_thread(gateway.inbox.submit, envelope)
        except Exception:
            log.warning("Failed to submit stop command for owner notification", exc_info=True)
    payload: JobResponse = {"job": job_detail(rows[0])}
    return JSONResponse(payload)


__all__ = ["collect_routes"]
