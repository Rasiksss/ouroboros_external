"""Container diagnostics: metadata only, with request correlation and no body logging."""

import json
import logging
import os
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


_REQUEST_ID: ContextVar[str] = ContextVar("finengine_request_id", default="")
_OPERATION_ID: ContextVar[str] = ContextVar("finengine_operation_id", default="")
log = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name, "pid": record.process,
            "request_id": _REQUEST_ID.get(), "operation_id": _OPERATION_ID.get(),
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
            payload["stack"] = [f"{frame.filename}:{frame.lineno}:{frame.name}"
                                for frame in traceback.extract_tb(record.exc_info[2])]
        return json.dumps(payload, ensure_ascii=True)


def configure_logging():
    logger = logging.getLogger("finengine")
    level = os.environ.get("FINENGINE_LOG_LEVEL", "INFO").strip().upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_finengine_owned", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._finengine_owned = True
    logger.addHandler(handler)
    # SDK DEBUG records can contain request bodies and authorization headers.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logger.info("event=logging_configured level=%s sink=stdout", logging.getLevelName(logger.level))


def safe_url(url):
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def log_failure(logger, event, error, started, **fields):
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    frames = [f"{frame.filename}:{frame.lineno}:{frame.name}"
              for frame in traceback.extract_tb(error.__traceback__)]
    logger.error(
        "event=%s error_type=%s status=%s duration_ms=%.1f stack=%s context=%s",
        event, type(error).__name__, status if isinstance(status, int) else None,
        (time.monotonic() - started) * 1000, json.dumps(frames), json.dumps(fields),
    )


@contextmanager
def operation(logger, event, **fields):
    operation_id = str(uuid.uuid4())
    token = _OPERATION_ID.set(operation_id)
    started = time.monotonic()
    logger.info("event=%s_start operation_id=%s context=%s", event, operation_id, json.dumps(fields))
    try:
        yield operation_id
    except Exception as error:
        log_failure(logger, f"{event}_failed", error, started, **fields)
        raise
    else:
        logger.info("event=%s_complete operation_id=%s duration_ms=%.1f",
                    event, operation_id, (time.monotonic() - started) * 1000)
    finally:
        _OPERATION_ID.reset(token)


def http_event_hooks():
    async def request_start(request):
        request.extensions["finengine_started"] = time.monotonic()
        log.info("event=http_out_start method=%s endpoint=%s", request.method, safe_url(request.url))

    async def response_received(response):
        started = response.request.extensions.get("finengine_started", time.monotonic())
        log.info("event=http_out_response method=%s endpoint=%s status=%d duration_ms=%.1f",
                 response.request.method, safe_url(response.request.url), response.status_code,
                 (time.monotonic() - started) * 1000)

    return {"request": [request_start], "response": [response_received]}


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = _REQUEST_ID.set(str(uuid.uuid4()))
        started = time.monotonic()
        status = None
        level = logging.DEBUG if scope.get("path") in ("/health", "/ready", "/metrics") else logging.INFO
        log.log(level, "event=http_start method=%s", scope.get("method"))

        async def logged_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                log.log(level, "event=http_response status=%s duration_ms=%.1f",
                        status, (time.monotonic() - started) * 1000)
            await send(message)

        try:
            await self.app(scope, receive, logged_send)
        except Exception as error:
            log_failure(log, "http_failed", error, started, response_status=status)
            raise
        else:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            log.log(level, "event=http_complete method=%s route=%s status=%s duration_ms=%.1f",
                    scope.get("method"), route, status, (time.monotonic() - started) * 1000)
        finally:
            _REQUEST_ID.reset(token)
