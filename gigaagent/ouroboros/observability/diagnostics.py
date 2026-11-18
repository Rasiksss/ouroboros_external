"""Request and model-call diagnostics without provider bodies or credentials."""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)


def safe_url(url: str) -> str:
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


@contextmanager
def operation(logger: logging.Logger, event: str, **fields: Any) -> Iterator[str]:
    operation_id = str(uuid.uuid4())
    started = time.monotonic()
    logger.info("event=%s_start operation_id=%s context=%s", event, operation_id, json.dumps(fields))
    try:
        yield operation_id
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        frames = [f"{frame.filename}:{frame.lineno}:{frame.name}"
                  for frame in traceback.extract_tb(error.__traceback__)]
        logger.error(
            "event=%s_failed operation_id=%s error_type=%s status=%s duration_ms=%.1f stack=%s",
            event, operation_id, type(error).__name__, status if isinstance(status, int) else None,
            (time.monotonic() - started) * 1000, json.dumps(frames),
        )
        raise
    else:
        logger.info("event=%s_complete operation_id=%s duration_ms=%.1f",
                    event, operation_id, (time.monotonic() - started) * 1000)


def log_response(logger: logging.Logger, operation_id: str, message: dict, usage: dict) -> None:
    input_tokens = usage.get("prompt_tokens", usage.get("in", 0))
    output_tokens = usage.get("completion_tokens", usage.get("out", 0))
    logger.info("event=llm_response operation_id=%s response_chars=%d tool_calls=%d input_tokens=%s output_tokens=%s",
                operation_id, len(str(message.get("content") or "")), len(message.get("tool_calls") or []),
                input_tokens if isinstance(input_tokens, int) else None,
                output_tokens if isinstance(output_tokens, int) else None)


class RequestLoggingMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        kind = scope["type"]
        status = None
        # Health probes are frequent; keep them available at DEBUG without flooding INFO.
        logger = logging.getLogger(log.name + ".probes") if scope.get("path") in {
            "/health", "/ready", "/metrics", "/healthz/live", "/healthz/ready"
        } else log
        level = logging.DEBUG if logger.name.endswith(".probes") else logging.INFO
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        logger.log(level, "event=%s_start request_id=%s method=%s", kind, request_id, scope.get("method"))

        async def logged_send(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "websocket.close":
                status = message.get("code", 1000)
            await send(message)

        try:
            await self.app(scope, receive, logged_send)
        except Exception as error:
            logger.error("event=%s_failed request_id=%s error_type=%s status=%s duration_ms=%.1f",
                         kind, request_id, type(error).__name__, status, (time.monotonic() - started) * 1000)
            raise
        else:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            logger.log(level, "event=%s_complete request_id=%s route=%s status=%s duration_ms=%.1f",
                       kind, request_id, route, status, (time.monotonic() - started) * 1000)
