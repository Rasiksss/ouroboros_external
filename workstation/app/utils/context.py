"""Context variables accessible from any layer (request, current user)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, TypeVar

T = TypeVar("T")


class Context[T]:
    """Typed wrapper around `ContextVar`."""

    def __init__(self, context_key: str, default: T | None = None) -> None:
        self._ctx_var: ContextVar[T | None] = ContextVar(context_key, default=default)

    def __call__(self) -> T | None:
        return self._ctx_var.get()

    def set(self, value: T) -> Token[Any]:
        return self._ctx_var.set(value)

    def reset(self, token: Token[Any]) -> None:
        self._ctx_var.reset(token)


request_context: Context[Any] = Context(context_key="request")
user_data_context: Context[Any] = Context(context_key="user_data")


# The default is shared by every context, so it must not be mutable: one request
# mutating it in place would leak its keys into all others. None means "unset".
logging_context: ContextVar[dict[str, Any] | None] = ContextVar("logging_context", default=None)


@contextmanager
def log_context(**kwargs) -> Generator[None]:
    current_ctx = dict(logging_context.get() or {})
    current_ctx.update(kwargs)
    token = logging_context.set(current_ctx)
    try:
        yield
    finally:
        logging_context.reset(token)
