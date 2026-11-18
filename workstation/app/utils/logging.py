"""Custom log formatters + config loader."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ecs_logging import StdlibFormatter
from opentelemetry.trace import get_current_span

from app.core.config import settings
from app.utils.context import logging_context


class LogLevelEnum(StrEnum):
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"
    DEBUG = "DEBUG"

    @property
    def level(self) -> int:
        return logging.getLevelName(self.value)


_ERROR_THRESHOLD = logging.ERROR
_MAX_LOG_STRING_LENGTH = 4096
_MAX_LOG_SEQUENCE_ITEMS = 50
_REDACTED = "[redacted]"
_SENSITIVE_KEY_MARKERS = {
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "bearer_token",
    "bearertoken",
    "refresh_token",
    "refreshtoken",
    "session_token",
    "sessiontoken",
    "token_hash",
    "tokenhash",
}
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token=)[^&\s]+"),
    re.compile(r"(?i)(password=)[^&\s]+"),
    re.compile(r"(?i)(secret=)[^&\s]+"),
    re.compile(r"(?i)(api[_-]?key=)[^&\s]+"),
)
_STANDARD_LOG_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__)


def _log_dir_is_writable(log_dir: Path) -> bool:
    """Probe write access, e.g. a readOnlyRootFilesystem container without a writable volume mounted at log_dir."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / f".write-check.{os.getpid()}"
        probe.touch(exist_ok=True)
        probe.unlink()
    except OSError:
        return False
    return True


def _disable_handler(loaded: dict[str, Any], name: str) -> None:
    loaded["handlers"].pop(name, None)
    configs = list(loaded.get("loggers", {}).values())
    if "root" in loaded:
        configs.append(loaded["root"])
    for logger_config in configs:
        if handlers := logger_config.get("handlers"):
            logger_config["handlers"] = [h for h in handlers if h != name]


def get_log_config(log_config: str | Path = "logging.json") -> dict[str, Any]:
    """Load a dictConfig JSON and apply level overrides from settings.

    APP_LOG_LEVEL overrides only the root logger; library loggers declared in
    the JSON keep their explicit levels. APP_LOG_LEVEL_CORE, when set, overrides
    the ``app`` logger so you can crank your own code to DEBUG without dragging
    along uvicorn / sqlalchemy / aiokafka noise.
    """
    root_level = LogLevelEnum(settings.APP_LOG_LEVEL).value
    core_level = (
        LogLevelEnum(settings.APP_LOG_LEVEL_CORE).value if settings.APP_LOG_LEVEL_CORE is not None else root_level
    )
    with Path(log_config).open() as file:
        loaded: dict[str, Any] = json.load(file)
    if "root" in loaded:
        loaded["root"]["level"] = root_level

    if file_handler := loaded["handlers"].get("file"):
        log_dir = Path(settings.APP_LOG_DIR)
        if _log_dir_is_writable(log_dir):
            # Fixed filename, opened in append mode: fluentbit tails one path,
            # and every APP_WORKERS process appends to the same file. POSIX
            # serializes each write() syscall against the file's inode, so
            # concurrent appends from multiple processes don't interleave
            # mid-line. No in-app rotation: only one process may ever own
            # rotation of a shared file, and nothing here elects that owner.
            file_handler["filename"] = str(log_dir / "app.log")
        else:
            logging.warning("Log directory %s is not writable; disabling file logging.", log_dir)
            _disable_handler(loaded, "file")
    loggers = loaded.setdefault("loggers", {})
    app_logger = loggers.setdefault("app", {"handlers": ["console"], "propagate": False})
    app_logger["level"] = core_level
    return loaded


def redact_log_value(value: Any, *, key: str | None = None) -> Any:
    """Return a logging-safe copy with obvious secrets masked and huge values bounded."""
    if key is not None and _is_sensitive_key(key):
        return _REDACTED

    if isinstance(value, str):
        redacted = value
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            redacted = pattern.sub(r"\1" + _REDACTED, redacted)
        if len(redacted) > _MAX_LOG_STRING_LENGTH:
            return f"{redacted[:_MAX_LOG_STRING_LENGTH]}...[truncated {len(redacted) - _MAX_LOG_STRING_LENGTH} chars]"
        return redacted

    if isinstance(value, bytes):
        return redact_log_value(value.decode("utf-8", errors="replace"), key=key)

    if isinstance(value, dict):
        return {
            str(item_key): redact_log_value(item_value, key=str(item_key)) for item_key, item_value in value.items()
        }

    if isinstance(value, tuple | list | set | frozenset):
        items = list(value)
        redacted_items = [redact_log_value(item) for item in items[:_MAX_LOG_SEQUENCE_ITEMS]]
        if len(items) > _MAX_LOG_SEQUENCE_ITEMS:
            redacted_items.append(f"...[truncated {len(items) - _MAX_LOG_SEQUENCE_ITEMS} items]")
        return redacted_items

    if value is None or isinstance(value, bool | int | float):
        return value

    return redact_log_value(str(value), key=key)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").lower()
    compact = normalized.replace("_", "")
    return normalized in _SENSITIVE_KEY_MARKERS or compact in _SENSITIVE_KEY_MARKERS


def _record_extra(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: redact_log_value(value, key=key)
        for key, value in record.__dict__.items()
        if key not in _STANDARD_LOG_RECORD_ATTRS and not key.startswith("_")
    }


def _exception_chain(exc: BaseException | None) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "message": str(redact_log_value(str(current))),
            }
        )
        current = current.__cause__ or current.__context__
    return chain


class ECSFormatter(StdlibFormatter):
    """
    ECS-style Formatter.

    See https://confluence.delta.sbrf.ru/display/AEF/LoggingFormat+AEF
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: Literal["%", "{", "$"] = "%",
        validate: bool | None = None,
        stack_trace_limit: int | None = None,
        extra: dict[str, Any] | None = None,
        exclude_fields: Sequence[str] = (),
        ensure_ascii: bool = True,
    ):
        agent_attributes = {
            "app.agent_id": settings.AGENT_ID,
            "app.agent_instance_id": settings.AGENT_INSTANCE_ID,
            "app.distributive": settings.DISTRIBUTIVE,
            "app.hub_id": settings.AI_HUB_ID,
            "app.hub_name": settings.HUB_NAME,
            "app.cluster_id": settings.CLUSTER_ID,
            "app.namespace": settings.POD_NAMESPACE,
            "app.pod_name": settings.POD_NAME,
            "service.name": settings.SERVICE_NAME,
        }
        kubernetes_attributes = {
            "kubernetes.cluster": settings.CLUSTER_ID,
            "kubernetes.namespace": settings.NAMESPACE,
            "kubernetes.node.name": settings.NODE_NAME,
            "kubernetes.pod.name": settings.POD_NAME,
        }
        if extra is None:
            extra = {}
        extra.update(
            **agent_attributes,
            **kubernetes_attributes,
        )
        super().__init__(fmt, datefmt, style, validate, stack_trace_limit, extra, exclude_fields, ensure_ascii)

    def format(self, record: logging.LogRecord) -> str:
        record = self._add_opentelemetry_attributes(record)
        record = self._add_request_attributes(record)
        return super().format(record)

    @classmethod
    def _add_opentelemetry_attributes(cls, record: logging.LogRecord) -> logging.LogRecord:
        span = get_current_span()
        span_context = span.get_span_context()
        record.__dict__.update(
            **{
                "span.id": f"{span_context.span_id:016x}",
                "trace.id": f"{span_context.trace_id:032x}",
            }
        )
        if (parent_ctx := getattr(span, "parent", None)) is not None:
            record.__dict__.update(**{"parent.id": f"{parent_ctx.span_id:016x}"})
        return record

    @classmethod
    def _add_request_attributes(cls, record: logging.LogRecord) -> logging.LogRecord:
        request_context = logging_context.get() or {}
        record.__dict__.update(request_context)
        return record

    def format_to_ecs(self, record: logging.LogRecord) -> dict[str, Any]:
        formatted = super().format_to_ecs(record)
        result = {}
        self._inverse_normalization(formatted, result)
        result_redacted = {key: redact_log_value(value, key=key) for key, value in result.items()}
        return result_redacted

    def _inverse_normalization(self, item: dict[str, Any], result: dict[str, Any], prefix: str = "") -> None:
        for k, v in item.items():
            subkey = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                self._inverse_normalization(v, result, subkey)
                continue
            result[subkey] = v


class JsonFormatter(logging.Formatter):
    """Structured JSON log records (single-line)."""

    def __init__(self, app: str = "service", oper_type: str = "app") -> None:
        super().__init__()
        self.app = app
        self.service = settings.SERVICE_NAME
        self.version = settings.APP_VERSION
        self.oper_type = oper_type

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "app": self.app,
            "service": self.service,
            "version": self.version,
            "operType": self.oper_type,
            "status": record.levelname,
            "body": redact_log_value(record.getMessage()),
            "timestamp": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "lineno": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }
        extra = _record_extra(record)
        if extra:
            data.update(extra)
        if record.levelno >= _ERROR_THRESHOLD and record.exc_info:
            data["exc_info"] = redact_log_value(self.formatException(record.exc_info))
            data["exc_chain"] = _exception_chain(record.exc_info[1])
        return json.dumps(data, ensure_ascii=False, default=str)


class PlainTextFormatter(logging.Formatter):
    """Human-readable single-line format."""

    def __init__(self) -> None:
        super().__init__()
        self.service = settings.SERVICE_NAME
        self.version = settings.APP_VERSION

    def format(self, record: logging.LogRecord) -> str:
        msg = " | ".join(
            [
                self.service,
                self.version,
                datetime.now(tz=UTC).strftime("%Y.%m.%d %H:%M:%S"),
                f"{record.levelname:^7}",
                f"{record.name}:{record.funcName}:{record.lineno}",
                f">> {redact_log_value(record.getMessage())}",
            ]
        )
        extra = _record_extra(record)
        if extra:
            msg += " | " + json.dumps(extra, ensure_ascii=False, default=str, sort_keys=True)
        if record.levelno >= _ERROR_THRESHOLD and record.exc_info:
            msg += f"\n{redact_log_value(self.formatException(record.exc_info))}"
        return msg
