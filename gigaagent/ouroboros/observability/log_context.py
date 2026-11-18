"""Logging context propagation via contextvars."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_TASK_ID: ContextVar[str] = ContextVar("ouroboros_task_id", default="")
_CORRELATION_ID: ContextVar[str] = ContextVar("ouroboros_correlation_id", default="")


def get_task_id() -> str:
    return _TASK_ID.get()


def get_correlation_id() -> str:
    return _CORRELATION_ID.get()


@contextmanager
def bind_log_context(*, task_id: str = "", correlation_id: str = "") -> Iterator[None]:
    task_token: Token[str] | None = None
    correlation_token: Token[str] | None = None
    try:
        if task_id:
            task_token = _TASK_ID.set(task_id)
        if correlation_id:
            correlation_token = _CORRELATION_ID.set(correlation_id)
        yield
    finally:
        if correlation_token is not None:
            _CORRELATION_ID.reset(correlation_token)
        if task_token is not None:
            _TASK_ID.reset(task_token)
