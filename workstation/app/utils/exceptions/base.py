"""Base class for typed API exceptions."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class BaseHTTPException(HTTPException):
    """Typed API exception with stable error code and details payload."""

    _message: str = "Что-то пошло не так"
    _code: str = "internal"
    _status_code: int = 500
    _headers: dict[str, str] | None = None

    def __init__(
        self,
        message: Any | None = None,
        *,
        details: dict[str, Any] | None = None,
        additional: str = "",
    ) -> None:
        resolved_message = message if message is not None else self._message
        headers = self._headers or {}
        if additional:
            resolved_message = f"{resolved_message} ({additional})"
        detail = {
            "code": self._code,
            "message": resolved_message,
            "details": details or {},
        }
        super().__init__(status_code=self._status_code, detail=detail, headers=headers)

    @property
    def code(self) -> str:
        return str(self.detail["code"])

    @property
    def message(self) -> str:
        return str(self.detail["message"])

    def __str__(self) -> str:
        return str(self.message)
