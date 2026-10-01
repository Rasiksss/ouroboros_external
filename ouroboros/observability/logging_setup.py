"""Central stdlib logging configuration."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import Literal

from ouroboros.observability.log_context import get_correlation_id, get_task_id

TEXT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_LEVEL = "INFO"

# Package roots of chatty third-party libraries: they log one level above the app so
# raising OUROBOROS_LOG_LEVEL to DEBUG stays readable. Descendants (`sqlalchemy.engine.
# Engine`, `dialog_bot_sdk.updates`) inherit through the logger hierarchy, including
# ones created after this runs, so listing the root is enough.
_QUIETED_LIBRARIES = ("dialog_bot_sdk", "sqlalchemy")
_STANDARD_LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)

LogFormat = Literal["json", "text"]


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = get_task_id()
        record.correlation_id = get_correlation_id()
        return True


class JsonLineFormatter(logging.Formatter):
    def __init__(self, *, include_traceback: bool = False) -> None:
        super().__init__()
        self._include_traceback = include_traceback

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        task_id = getattr(record, "task_id", "") or ""
        if task_id:
            payload["task_id"] = str(task_id)
        correlation_id = getattr(record, "correlation_id", "") or ""
        if correlation_id:
            payload["correlation_id"] = str(correlation_id)
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] is not None else "Exception"
            exc_msg = str(record.exc_info[1] or "")
            payload["exception"] = f"{exc_type}: {exc_msg}".strip()
            if self._include_traceback:
                payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def resolve_log_level(level: str | None) -> int:
    if level:
        raw = str(level).strip().upper()
    else:
        from ouroboros.storage.settings.store import load_settings

        raw = str(load_settings().OUROBOROS_LOG_LEVEL or _DEFAULT_LEVEL).strip().upper()
    if not raw:
        raw = _DEFAULT_LEVEL
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw)
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def next_level_up(level: int) -> int:
    """Next standard logging level above `level`, capped at CRITICAL."""
    for candidate in _STANDARD_LEVELS:
        if candidate > level:
            return candidate
    return logging.CRITICAL


def resolve_log_format(log_format: str | None) -> LogFormat:
    if log_format:
        raw = str(log_format).strip().lower()
    else:
        from ouroboros.storage.settings.store import load_settings

        raw = str(load_settings().OUROBOROS_LOG_FORMAT or "json").strip().lower()
    if raw == "text":
        return "text"
    return "json"


def configure_logging(
    *,
    level: str | None = None,
    log_format: str | None = None,
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root_level = resolve_log_level(level)
    root.setLevel(root_level)

    library_level = next_level_up(root_level)
    for name in _QUIETED_LIBRARIES:
        logging.getLogger(name).setLevel(library_level)

    chosen_format = resolve_log_format(log_format)
    from ouroboros.storage.settings.store import load_settings

    include_traceback = bool(load_settings().OUROBOROS_LOG_TRACEBACK)
    formatter: logging.Formatter = (
        JsonLineFormatter(include_traceback=include_traceback)
        if chosen_format == "json"
        else logging.Formatter(TEXT_LOG_FORMAT)
    )
    context_filter = LogContextFilter()

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(context_filter)
    root.addHandler(stream_handler)
