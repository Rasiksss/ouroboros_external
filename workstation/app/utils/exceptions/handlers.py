"""Global exception handlers mapping internal errors to API responses."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from logging import getLogger
from typing import Any

import pydantic
from fastapi import Request, status
from fastapi import exceptions as fastapi_exceptions
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, NoResultFound, SQLAlchemyError

from app.core.config import settings
from app.schemas.common import ErrorResponse
from app.utils.exceptions.base import BaseHTTPException
from app.utils.exceptions.common import (
    INTERNAL_SERVER_ERROR_DESC,
    AuthRequiredException,
    NotFoundException,
    PolicyDeniedException,
    ValidationErrorException,
)

logger = getLogger(__name__)


def _error(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    payload = ErrorResponse(
        code=code,
        message=message,
        details=details or {},
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _request_diagnostics(
    request: Request,
    exception: Exception,
    *,
    status_code: int,
    code: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client = request.client.host if request.client is not None else None
    return {
        "phase": "api-error",
        "method": request.method,
        "path": request.url.path,
        "statusCode": status_code,
        "code": code,
        "exceptionType": type(exception).__name__,
        "exceptionMessage": str(exception),
        "details": details or {},
        "client": client,
    }


def _log_exception(
    request: Request,
    exception: Exception,
    *,
    status_code: int,
    code: str,
    details: dict[str, Any] | None = None,
    log_method: Callable[..., None] | None = logger.error,
) -> None:
    if log_method is None:
        return

    exc_info = (type(exception), exception, exception.__traceback__) if status_code >= 500 else None
    log_method(
        "request_failed status_code=%s code=%s path=%s",
        status_code,
        code,
        request.url.path,
        exc_info=exc_info,
        extra={
            "event.category": "request",
            "event.action": "failure",
            "app.operation": "_log_exception",
            "event.outcome": "failure",
            "app.status": "failed",
            "error.type": type(exception).__name__,
            "error.message": str(exception),
            **(
                {"error.stack_trace": "".join(traceback.format_exception(exc_info[0], exc_info[1], exc_info[2]))}
                if exc_info is not None
                else {}
            ),
            "diagnostics": _request_diagnostics(
                request,
                exception,
                status_code=status_code,
                code=code,
                details=details,
            ),
        },
    )


def _handle(
    request: Request,
    exception: Any,
    *,
    status_code: int,
    code: str,
    censored_message: str,
    details: dict[str, Any] | None = None,
    log_method: Callable[..., None] | None = logger.error,
) -> JSONResponse:
    _log_exception(
        request,
        exception,
        status_code=status_code,
        code=code,
        details=details,
        log_method=log_method,
    )
    message = censored_message if settings.IS_ERR_DETAIL_HIDDEN else str(exception)
    return _error(status_code=status_code, code=code, message=message, details=details)


def handle_api_error(request: Request, exception: BaseHTTPException) -> JSONResponse:
    status_code = exception.status_code
    log_method = logger.error if status_code >= 500 else logger.warning
    _log_exception(
        request,
        exception,
        status_code=status_code,
        code=exception.code,
        details=exception.detail.get("details", {}),
        log_method=log_method,
    )
    return JSONResponse(status_code=exception.status_code, content=exception.detail)


def handle_integrity_error(request: Request, exception: IntegrityError) -> JSONResponse:
    return _handle(
        request,
        exception,
        status_code=status.HTTP_400_BAD_REQUEST,
        code="bad-request",
        censored_message="Нарушена целостность данных",
    )


def handle_not_found_error(request: Request, exception: NoResultFound) -> JSONResponse:
    return _handle(
        request,
        exception,
        status_code=status.HTTP_404_NOT_FOUND,
        code="not-found",
        censored_message="Запрашиваемый ресурс не найден",
        log_method=logger.warning,
    )


def handle_sqlalchemy_error(request: Request, exception: SQLAlchemyError) -> JSONResponse:
    return _handle(
        request,
        exception,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal",
        censored_message=INTERNAL_SERVER_ERROR_DESC,
    )


def handle_validation_error(
    request: Request,
    exception: ValueError | pydantic.ValidationError | fastapi_exceptions.RequestValidationError,
) -> JSONResponse:
    details: dict[str, Any] = {}
    if hasattr(exception, "errors"):
        try:
            raw_errors = exception.errors(include_url=False)  # type: ignore[call-arg]
        except TypeError:
            raw_errors = exception.errors()  # type: ignore[call-arg]
        details["errors"] = jsonable_encoder(raw_errors)
    return _handle(
        request,
        exception,
        status_code=status.HTTP_400_BAD_REQUEST,
        code="bad-request",
        censored_message=ValidationErrorException().message,
        details=details,
    )


def handle_any_error(request: Request, exception: Exception) -> JSONResponse:
    return _handle(
        request,
        exception,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal",
        censored_message=INTERNAL_SERVER_ERROR_DESC,
    )


exception_handlers: tuple[tuple[type[Exception], Callable[..., Any]], ...] = (
    (BaseHTTPException, handle_api_error),
    (IntegrityError, handle_integrity_error),
    (NoResultFound, handle_not_found_error),
    (SQLAlchemyError, handle_sqlalchemy_error),
    (pydantic.ValidationError, handle_validation_error),
    (fastapi_exceptions.RequestValidationError, handle_validation_error),
    (AuthRequiredException, handle_api_error),
    (PolicyDeniedException, handle_api_error),
    (NotFoundException, handle_api_error),
    (ValueError, handle_validation_error),
    (Exception, handle_any_error),
)
