"""Concrete API exceptions used across the workstation service."""

from __future__ import annotations

from typing import ClassVar

from fastapi import status

from app.utils.exceptions.base import BaseHTTPException

BAD_REQUEST_DESC = "Ошибка валидации входных параметров"
UNAUTHORIZED_DESC = "Сессионная аутентификация не передана или невалидна"
FORBIDDEN_DESC = "Операция запрещена политикой сервиса"
NOT_FOUND_DESC = "Запрашиваемый ресурс не найден"
INVALID_STATE_DESC = "Операция не разрешена в текущем состоянии сессии"
QUOTA_EXCEEDED_DESC = "Превышены ресурсные ограничения сессии"
TIMEOUT_DESC = "Время выполнения операции истекло"
INTERNAL_SERVER_ERROR_DESC = "На сервере возникла необработанная ошибка"


class InternalServerErrorException(BaseHTTPException):
    _message = INTERNAL_SERVER_ERROR_DESC
    _code = "internal"
    _status_code = status.HTTP_500_INTERNAL_SERVER_ERROR


class AuthRequiredException(BaseHTTPException):
    _message = UNAUTHORIZED_DESC
    _code = "auth-required"
    _status_code = status.HTTP_401_UNAUTHORIZED
    _headers: ClassVar[dict[str, str]] = {"WWW-Authenticate": "Bearer"}


AUTH_REQUIRED_MESSAGES: dict[str, str] = {
    "missing-or-malformed-bearer": "Не передан bearer-токен или неверный формат Authorization",
    "empty-bearer": "Bearer-токен пустой",
    "missing-session-id": "Не передан X-Session-Id",
    "missing-auth-context": "Не удалось прочитать сессионную аутентификацию из запроса",
    "session-id-mismatch": "X-Session-Id не совпадает с session_id в path",
    "token-mismatch": "Bearer-токен не соответствует сессии",
    "missing-session-create-secret": "Не передан секрет создания сессии",
    "session-create-secret-mismatch": "Неверный секрет создания сессии",
}


def auth_required(reason: str) -> AuthRequiredException:
    return AuthRequiredException(AUTH_REQUIRED_MESSAGES.get(reason, UNAUTHORIZED_DESC), details={"reason": reason})


class PolicyDeniedException(BaseHTTPException):
    _message = FORBIDDEN_DESC
    _code = "policy-deny"
    _status_code = status.HTTP_403_FORBIDDEN


class NotFoundException(BaseHTTPException):
    _message = NOT_FOUND_DESC
    _code = "not-found"
    _status_code = status.HTTP_404_NOT_FOUND


class ValidationErrorException(BaseHTTPException):
    _message = BAD_REQUEST_DESC
    _code = "bad-request"
    _status_code = status.HTTP_400_BAD_REQUEST


class InvalidStateException(BaseHTTPException):
    _message = INVALID_STATE_DESC
    _code = "invalid-state"
    _status_code = status.HTTP_409_CONFLICT


class QuotaExceededException(BaseHTTPException):
    _message = QUOTA_EXCEEDED_DESC
    _code = "quota-exceeded"
    _status_code = status.HTTP_413_CONTENT_TOO_LARGE


class TimeoutException(BaseHTTPException):
    _message = TIMEOUT_DESC
    _code = "timeout"
    _status_code = status.HTTP_504_GATEWAY_TIMEOUT
