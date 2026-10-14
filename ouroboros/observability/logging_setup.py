"""Central stdlib logging configuration."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ouroboros.storage.settings.schema import AppSettings

from ouroboros.observability.aef_tracing import current_trace_id
from ouroboros.observability.log_context import get_correlation_id, get_task_id

TEXT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_LEVEL = "INFO"

# Package roots of chatty third-party libraries: they log one level above the app so
# raising OUROBOROS_LOG_LEVEL to DEBUG stays readable. Descendants (`sqlalchemy.engine.
# Engine`, `dialog_bot_sdk.updates`) inherit through the logger hierarchy, including
# ones created after this runs, so listing the root is enough.
_QUIETED_LIBRARIES = ("dialog_bot_sdk", "sqlalchemy")
_KAFKA_LIBRARY = "kafka"
_KAFKA_RECONNECT_LOGGERS = (
    "kafka.conn",
    "kafka.client",
    "kafka.coordinator",
    "kafka.coordinator.heartbeat",
    "kafka.coordinator.consumer",
    "kafka.producer.sender",
    "kafka.sasl.plain",
    "kafka.sasl.scram",
)
_STANDARD_LEVELS = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)

LogFormat = Literal["json", "text"]
LogSink = Literal["stdout", "file", "both"]

# The fluent-bit sidecar tails app.log only, so it keeps the full stream.
LOG_FILE_NAME = "app.log"
ERROR_LOG_FILE_NAME = "error.log"
# Reconfiguration closes only handlers carrying this flag; the rest belong to
# pytest or a library that would silently reopen them.
_OWNED = "_ouroboros_logging_owned"


class LogContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = get_task_id()
        record.correlation_id = get_correlation_id()
        record.trace_id = current_trace_id()
        return True


class _KafkaReconnectGate(logging.Filter):
    def __init__(self, *, interval_sec: float) -> None:
        super().__init__()
        self._interval_sec = interval_sec
        self._last_passed: dict[tuple[str, int, str], float] = {}
        self._lock = threading.Lock()

    @classmethod
    def install(cls, *, interval_sec: float) -> None:
        gate = cls(interval_sec=interval_sec)
        for name in _KAFKA_RECONNECT_LOGGERS:
            logger = logging.getLogger(name)
            for installed in [entry for entry in logger.filters if isinstance(entry, cls)]:
                logger.removeFilter(installed)
            logger.addFilter(gate)

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, str(record.msg))
        now = time.monotonic()
        with self._lock:
            last_passed = self._last_passed.get(key)
            if last_passed is not None and now - last_passed < self._interval_sec:
                return False
            self._last_passed[key] = now
            return True


class JsonLineFormatter(logging.Formatter):
    def __init__(self, *, include_traceback: bool = False, include_callsite: bool = False) -> None:
        super().__init__()
        self._include_traceback = include_traceback
        self._include_callsite = include_callsite

    def format(self, record: logging.LogRecord) -> str:
        # Key and shape match the fluent-bit `default_json` parser; from
        # record.created so every handler stamps one record identically.
        created = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, object] = {
            "time": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
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
        # The operation identifier joins the other two rather than replacing either: they answer
        # different questions and are read together when an incident is reconstructed.
        trace_id = getattr(record, "trace_id", "") or ""
        if trace_id:
            payload["trace_id"] = str(trace_id)
        if self._include_callsite:
            # pathname, not filename: several core.py/app.py share a basename.
            payload["file"] = record.pathname
            payload["line"] = record.lineno
            payload["func"] = record.funcName
        if record.exc_info:
            exc_type = record.exc_info[0].__name__ if record.exc_info[0] is not None else "Exception"
            exc_msg = str(record.exc_info[1] or "")
            payload["exception"] = f"{exc_type}: {exc_msg}".strip()
            if self._include_traceback:
                payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        # Escaped, not raw UTF-8: the record is read by a collector and a search index that
        # do not necessarily decode as UTF-8, and a mangled message is one that cannot be
        # searched for — which costs most in the incident it was written for. `text` format
        # is what a developer tails locally and stays readable.
        return json.dumps(payload, ensure_ascii=True)


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


def resolve_log_sink(sink: str | None) -> LogSink:
    from ouroboros.storage.settings.schema import normalize_log_sink

    if not sink:
        from ouroboros.storage.settings.store import load_settings

        return load_settings().OUROBOROS_LOG_SINK
    return normalize_log_sink(sink)


class _FallsBackToStdout(logging.Handler):
    """A file handler that gives up its sink instead of the records.

    The disk can fill or the volume turn read-only long after the file opened,
    and stdlib would then drop every later record into `handleError`.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        root = logging.getLogger()
        if self not in root.handlers:
            return
        root.removeHandler(self)
        self.close()
        if not any(getattr(h, _OWNED, False) for h in root.handlers):
            stdout = logging.StreamHandler(stream=sys.stdout)
            stdout.setFormatter(self.formatter)
            for log_filter in self.filters:
                stdout.addFilter(log_filter)
            setattr(stdout, _OWNED, True)
            root.addHandler(stdout)
        _warn("File log sink failed while writing, falling back to stdout: %s", sys.exc_info()[1])
        root.handle(record)


class _RotatingFileHandler(_FallsBackToStdout, logging.handlers.RotatingFileHandler):
    pass


class _TimedRotatingFileHandler(_FallsBackToStdout, logging.handlers.TimedRotatingFileHandler):
    pass


def _file_handler(settings: AppSettings, path: Path) -> logging.Handler:
    if settings.OUROBOROS_LOG_ROTATION == "time":
        return _TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=settings.OUROBOROS_LOG_FILE_BACKUP_COUNT,
            utc=True,
            encoding="utf-8",
        )
    return _RotatingFileHandler(
        path,
        maxBytes=settings.OUROBOROS_LOG_FILE_MAX_BYTES,
        # 0 would rotate by reopening without truncating: unbounded growth.
        backupCount=max(1, settings.OUROBOROS_LOG_FILE_BACKUP_COUNT),
        encoding="utf-8",
    )


def _warn(message: str, *args: object) -> None:
    """A WARNING a raised OUROBOROS_LOG_LEVEL cannot hide: handed to the handlers
    directly, because it is the only trace that the sink is not what was asked for.
    With none attached yet it reaches stderr through logging's lastResort."""
    logger = logging.getLogger(__name__)
    logger.handle(logger.makeRecord(logger.name, logging.WARNING, __file__, 0, message, args, None))


def configure_logging(
    *,
    level: str | None = None,
    log_format: str | None = None,
    sink: str | None = None,
) -> LogSink:
    """Configure the root logger; returns the sink actually in use."""
    root = logging.getLogger()
    # Detached before closing: a closed handler still on root reopens its file
    # on the next record. Runs more than once per boot, and each holds an fd.
    dropped = [handler for handler in root.handlers if getattr(handler, _OWNED, False)]
    root.handlers.clear()
    for handler in dropped:
        handler.close()
    root_level = resolve_log_level(level)
    root.setLevel(root_level)

    library_level = next_level_up(root_level)
    for name in _QUIETED_LIBRARIES:
        logging.getLogger(name).setLevel(library_level)
    from ouroboros.contracts.stream_replies import BROKER_REPORT_INTERVAL_SEC

    logging.getLogger(_KAFKA_LIBRARY).setLevel(max(logging.INFO, library_level))
    _KafkaReconnectGate.install(interval_sec=BROKER_REPORT_INTERVAL_SEC)

    chosen_format = resolve_log_format(log_format)
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    formatter: logging.Formatter = (
        JsonLineFormatter(
            include_traceback=bool(settings.OUROBOROS_LOG_TRACEBACK),
            include_callsite=root_level <= logging.DEBUG,
        )
        if chosen_format == "json"
        else logging.Formatter(TEXT_LOG_FORMAT)
    )

    chosen_sink = resolve_log_sink(sink)
    if chosen_sink != "stdout" and chosen_format == "text":
        # Before the handlers exist, so it lands on stderr rather than in the
        # very file the shipper cannot parse.
        _warn("OUROBOROS_LOG_FORMAT=text writes non-JSON lines the log shipper cannot parse")

    handlers: list[logging.Handler] = []
    file_error: OSError | None = None
    if chosen_sink != "stdout":
        try:
            log_dir = Path(settings.OUROBOROS_LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(_file_handler(settings, log_dir / LOG_FILE_NAME))
            if settings.OUROBOROS_LOG_ERROR_FILE_ENABLED:
                error_handler = _file_handler(settings, log_dir / ERROR_LOG_FILE_NAME)
                error_handler.setLevel(logging.ERROR)
                handlers.append(error_handler)
        except OSError as exc:
            # All or nothing: a half-open file sink would ship every line twice.
            for handler in handlers:
                handler.close()
            handlers.clear()
            file_error, chosen_sink = exc, "stdout"
    if chosen_sink != "file":
        handlers.append(logging.StreamHandler(stream=sys.stdout))

    context_filter = LogContextFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context_filter)
        setattr(handler, _OWNED, True)
        root.addHandler(handler)

    if file_error is not None:
        _warn("File log sink unavailable, falling back to stdout: %s", file_error)
    return chosen_sink
