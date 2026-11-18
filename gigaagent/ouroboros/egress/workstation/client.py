"""Raw AEF WorkStation protocol client: 8 routes, headers, retry, error mapping.

Synchronous ``httpx.Client`` throughout (spec §4 has no long-lived connection
to justify an asyncio bridge, unlike the A2A/MCP egress managers — see
``ouroboros/egress/a2a/manager.py``, whose bridge exists only because its
client is an ``httpx.AsyncClient`` owned by the lifespan loop).
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import httpx

from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header

log = logging.getLogger(__name__)

_PROTOCOL_HEADER = "X-Workstation-Protocol"
_SESSION_HEADER = "X-Session-Id"
_AUTH_HEADER = "Authorization"
_CREATE_SECRET_HEADER = "X-Session-Create-Secret"
_PROTOCOL_VERSION = "1"

#: A streamed exec's read timeout must outlive the requested ``timeoutSec``
#: (spec §12.3) — otherwise the HTTP connection is torn down by the client
#: before the service's own timeout has a chance to produce the final
#: ``exit`` record.
_STREAM_TIMEOUT_MARGIN_SEC = 30.0

#: Error bodies are the service's own compact `{code, message, details}`
#: (spec §7.1), not a large arbitrary document — a generous but still
#: bounded cap is enough to avoid an unbounded string in logs/exceptions.
_MAX_ERROR_BODY_CHARS = 2_000


class SessionStatus(str, Enum):
    """The session lifecycle (spec §3.1) — a closed set, no "other" row.

    An unrecognized value from the wire has no defined meaning and is a
    contract violation: ``_parse_session_status`` below raises
    ``WorkstationProtocolError`` rather than accepting it.
    """

    PROVISIONING = "provisioning"
    READY = "ready"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class ErrorCode(str, Enum):
    """The error ``code`` (spec §7.1). The spec's own table lists a final

    "прочее/`internal`" row, so any code outside the other seven means
    ``internal`` at the protocol level: ``_parse_error_code`` below folds an
    unrecognized wire value into ``INTERNAL`` rather than raising.
    """

    BAD_REQUEST = "bad-request"
    AUTH_REQUIRED = "auth-required"
    NOT_FOUND = "not-found"
    INVALID_STATE = "invalid-state"
    POLICY_DENY = "policy-deny"
    QUOTA_EXCEEDED = "quota-exceeded"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


def _parse_session_status(raw: Any, *, operation: str) -> SessionStatus:
    text = str(raw or "")
    try:
        return SessionStatus(text)
    except ValueError:
        raise WorkstationProtocolError(
            f"WorkStation {operation} returned an unrecognized session status {text!r}"
        ) from None


def _parse_error_code(raw: Any) -> ErrorCode:
    text = str(raw or "internal")
    try:
        return ErrorCode(text)
    except ValueError:
        return ErrorCode.INTERNAL


class WorkstationError(Exception):
    """Base for all WorkStation client/protocol errors."""


class WorkstationTransportError(WorkstationError):
    """A request never got a response (network failure/timeout), after retries."""


class WorkstationApiError(WorkstationError):
    """A non-2xx response carrying the spec §7.1 ``{code, message, details}`` body."""

    def __init__(self, *, operation: str, status_code: int, code: ErrorCode, message: str, details: Any = None) -> None:
        self.operation = operation
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"WorkStation {operation} failed: HTTP {status_code} {code.value}: {message}")

    @property
    def is_not_found(self) -> bool:
        return self.code == ErrorCode.NOT_FOUND

    @property
    def is_auth_required(self) -> bool:
        return self.code == ErrorCode.AUTH_REQUIRED

    @property
    def is_invalid_state(self) -> bool:
        return self.code == ErrorCode.INVALID_STATE

    @property
    def is_quota_exceeded(self) -> bool:
        return self.code == ErrorCode.QUOTA_EXCEEDED


class WorkstationProtocolError(WorkstationError):
    """A successful response did not match the protocol contract (spec §18)."""


class WorkstationAccessDenied(WorkstationError):
    """A file-bridge write was refused: workspace access level is not `rw` (spec §8.4)."""


@dataclass(frozen=True)
class WorkstationConfig:
    base_url: str
    create_secret: str = field(repr=False)
    session_strategy: Literal["ephemeral", "recover"] = "ephemeral"
    idle_timeout_sec: int = 900
    absolute_ttl_sec: int = 3600
    command_timeout_sec: int = 300
    http_timeout_sec: float = 30.0
    max_attempts: int = 3
    retry_base_delay_sec: float = 0.1
    workspace_root: str = "/workspace"
    access_level: Literal["rw", "ro", "none"] = "rw"
    attachment_max_bytes: int = 20 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Any) -> WorkstationConfig | None:
        """``None`` when ``WORKSTATION_BASE_URL`` is empty — the integration is off."""
        base_url = settings.WORKSTATION_BASE_URL.strip()
        if not base_url:
            return None
        return cls(
            base_url=base_url.rstrip("/"),
            create_secret=settings.WORKSTATION_CREATE_SECRET.get_secret_value(),
            session_strategy=settings.WORKSTATION_SESSION_STRATEGY,
            idle_timeout_sec=int(settings.WORKSTATION_IDLE_TIMEOUT_SEC),
            absolute_ttl_sec=int(settings.WORKSTATION_ABSOLUTE_TTL_SEC),
            command_timeout_sec=int(settings.WORKSTATION_COMMAND_TIMEOUT_SEC),
            http_timeout_sec=float(settings.WORKSTATION_HTTP_TIMEOUT_SEC),
            max_attempts=max(1, int(settings.WORKSTATION_MAX_ATTEMPTS)),
            retry_base_delay_sec=max(0.0, float(settings.WORKSTATION_RETRY_BASE_DELAY_MS) / 1000.0),
            workspace_root=str(settings.WORKSTATION_WORKSPACE_ROOT or "/workspace"),
            access_level=settings.WORKSTATION_ACCESS_LEVEL,
            attachment_max_bytes=int(settings.WORKSTATION_ATTACHMENT_MAX_BYTES),
        )


@dataclass(frozen=True)
class CreateSessionResult:
    session_id: str
    status: SessionStatus
    token: str = field(repr=False)
    created: bool
    restored: bool
    quotas: dict[str, Any]


@dataclass(frozen=True)
class SessionState:
    session_id: str
    status: SessionStatus
    quotas: dict[str, Any]


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class UploadResult:
    path: str
    bytes_written: int
    sha256: str


def _exec_body(
    command: list[str],
    cwd: str,
    env: Mapping[str, str] | None,
    stdin: bytes | None,
    timeout_sec: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "command": command,
        "cwd": cwd,
        "env": dict(env or {}),
        "timeoutSec": timeout_sec,
    }
    if stdin is not None:
        body["stdin"] = base64.b64encode(stdin).decode("ascii")
        body["stdinEncoding"] = "base64"
    else:
        body["stdinEncoding"] = "utf8"
    return body


def _json_object(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorkstationProtocolError(f"WorkStation {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WorkstationProtocolError(f"WorkStation {operation} response must be a JSON object")
    return payload


class WorkstationClient:
    """Owns the one cached ``httpx.Client`` for this process — one WS target, one slot."""

    def __init__(self, config: WorkstationConfig, *, client_factory: Callable[..., httpx.Client] | None = None) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._client_factory = client_factory or httpx.Client

    # -- transport -----------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory(timeout=self._config.http_timeout_sec)
            assert self._client is not None
            return self._client

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                log.warning("Failed to close WorkStation HTTP client", exc_info=True)

    def _headers(self, session_id: str, *, token: str | None, create: bool) -> dict[str, str]:
        """The per-request header set, rebuilt on every call — never merged into the cached client.

        ``X-Trace-Id`` says which operation and ``X-Agent-Id`` which agent; a request carrying one
        but not the other is half of a correlation. Both are read here rather than at construction
        because the client outlives every operation, so a value fixed once would attribute every
        later call to the first one. Each is omitted when nothing is active: an absent header is
        what the mesh records as "nobody said", an empty one is a value that means nothing.
        """
        headers = with_agent_header(with_trace_header({}))
        headers[_PROTOCOL_HEADER] = _PROTOCOL_VERSION
        headers[_SESSION_HEADER] = session_id
        if create:
            if self._config.create_secret:
                headers[_CREATE_SECRET_HEADER] = self._config.create_secret
        else:
            headers[_AUTH_HEADER] = f"Bearer {token or ''}"
        return headers

    def _api_error(self, response: httpx.Response, operation: str) -> WorkstationApiError:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = _parse_error_code(payload.get("code"))
            message = str(payload.get("message") or "")
            details = payload.get("details")
        else:
            code = ErrorCode.INTERNAL
            message = self._bounded_text(response)
            details = None
        return WorkstationApiError(
            operation=operation, status_code=response.status_code, code=code, message=message, details=details
        )

    @staticmethod
    def _bounded_text(response: httpx.Response) -> str:
        try:
            return response.text[:_MAX_ERROR_BODY_CHARS]
        except Exception:
            return "<unavailable>"

    def _exec_timeout(self, timeout_sec: int) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self._config.http_timeout_sec,
            read=float(timeout_sec) + _STREAM_TIMEOUT_MARGIN_SEC,
            write=self._config.http_timeout_sec,
            pool=self._config.http_timeout_sec,
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        session_id: str,
        token: str | None = None,
        create: bool = False,
        operation: str,
        timeout: httpx.Timeout | float | None = None,
        **request_kwargs: Any,
    ) -> httpx.Response:
        """One protocol call with the §7.2 policy: retry transport failures and
        HTTP >=500 up to ``max_attempts``, exponential backoff; 4xx is raised
        immediately and never retried."""
        client = self._get_client()
        url = f"{self._config.base_url}{path}"
        headers = self._headers(session_id, token=token, create=create)
        effective_timeout = timeout if timeout is not None else self._config.http_timeout_sec
        attempts = max(1, self._config.max_attempts)
        last_error: WorkstationError | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = client.request(method, url, headers=headers, timeout=effective_timeout, **request_kwargs)
            except httpx.HTTPError as exc:
                last_error = WorkstationTransportError(
                    f"WorkStation {operation} transport failure: {type(exc).__name__}: {exc}"
                )
            else:
                if response.status_code < 300:
                    return response
                if response.status_code < 500:
                    raise self._api_error(response, operation)
                last_error = self._api_error(response, operation)
            if attempt < attempts:
                time.sleep(self._config.retry_base_delay_sec * (2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error

    # -- protocol operations (spec §2) ----------------------------------------------

    def create_session(self, session_id: str, *, timeout_sec: float | None = None) -> CreateSessionResult:
        body = {
            "idleTimeoutSec": self._config.idle_timeout_sec,
            "absoluteTtlSec": self._config.absolute_ttl_sec,
        }
        response = self._send(
            "POST",
            "/v1/sessions",
            session_id=session_id,
            create=True,
            operation="create_session",
            json=body,
            timeout=timeout_sec,
        )
        payload = _json_object(response, operation="create_session")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise WorkstationProtocolError(f"WorkStation create_session for {session_id!r} returned no token")
        quotas = payload.get("quotas")
        return CreateSessionResult(
            session_id=str(payload.get("id") or session_id),
            status=_parse_session_status(payload.get("status"), operation="create_session"),
            token=token,
            created=response.status_code == 201,
            restored=bool(payload.get("workspacePersistence")),
            quotas=quotas if isinstance(quotas, dict) else {},
        )

    def get_session(self, session_id: str, token: str, *, timeout_sec: float | None = None) -> SessionState:
        response = self._send(
            "GET",
            f"/v1/sessions/{session_id}",
            session_id=session_id,
            token=token,
            operation="get_session",
            timeout=timeout_sec,
        )
        payload = _json_object(response, operation="get_session")
        quotas = payload.get("quotas")
        return SessionState(
            session_id=str(payload.get("id") or session_id),
            status=_parse_session_status(payload.get("status"), operation="get_session"),
            quotas=quotas if isinstance(quotas, dict) else {},
        )

    def exec_buffered(
        self,
        *,
        session_id: str,
        token: str,
        command: list[str],
        cwd: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_sec: int,
    ) -> ExecResult:
        body = _exec_body(command, cwd, env, stdin, timeout_sec)
        response = self._send(
            "POST",
            f"/v1/sessions/{session_id}/exec",
            session_id=session_id,
            token=token,
            operation="exec",
            json=body,
            timeout=self._exec_timeout(timeout_sec),
        )
        payload = _json_object(response, operation="exec")
        return ExecResult(
            exit_code=int(payload.get("exitCode") or 0),
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            timed_out=bool(payload.get("timedOut")),
        )

    def exec_stream(
        self,
        *,
        session_id: str,
        token: str,
        command: list[str],
        cwd: str,
        env: Mapping[str, str] | None,
        stdin: bytes | None,
        timeout_sec: int,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        """Consume the NDJSON stream of ``exec:stream`` (spec §12.2).

        Deliberately a single attempt, not routed through ``_send``'s retry:
        by the time a 5xx or transport failure surfaces here the command may
        already be running or have partially run on the service, and a
        silent retry would risk running it twice. Spec §5's closing note
        ("no transparent recreate-and-retry inside an already started
        operation") points the same way — surface the error as-is instead.
        """
        body = _exec_body(command, cwd, env, stdin, timeout_sec)
        url = f"{self._config.base_url}/v1/sessions/{session_id}/exec:stream"
        headers = self._headers(session_id, token=token, create=False)
        timeout = self._exec_timeout(timeout_sec)
        client = self._get_client()
        exit_seen = False
        with client.stream("POST", url, headers=headers, json=body, timeout=timeout) as response:
            if response.status_code >= 400:
                response.read()
                raise self._api_error(response, "exec:stream")
            for line in response.iter_lines():
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except ValueError as exc:
                    raise WorkstationProtocolError(
                        f"WorkStation exec:stream for {session_id!r} emitted a non-JSON line"
                    ) from exc
                if not isinstance(event, dict):
                    continue
                on_event(event)
                if event.get("kind") == "exit":
                    exit_seen = True
        if not exit_seen:
            raise WorkstationProtocolError(
                f"WorkStation exec:stream for {session_id!r} ended without a terminal 'exit' record"
            )

    def upload_file(
        self, *, session_id: str, token: str, path: str, data: bytes, filename: str, mime: str
    ) -> UploadResult:
        response = self._send(
            "POST",
            f"/v1/sessions/{session_id}/files:upload",
            session_id=session_id,
            token=token,
            operation="files:upload",
            data={"path": path},
            files={"file": (filename, data, mime)},
        )
        payload = _json_object(response, operation="files:upload")
        return UploadResult(
            path=str(payload.get("path") or path),
            bytes_written=int(payload.get("bytes") or 0),
            sha256=str(payload.get("sha256") or ""),
        )

    def download_file(self, *, session_id: str, token: str, path: str) -> bytes:
        response = self._send(
            "POST",
            f"/v1/sessions/{session_id}/files:download",
            session_id=session_id,
            token=token,
            operation="files:download",
            json={"path": path},
        )
        return response.content

    def list_files(
        self,
        *,
        session_id: str,
        token: str,
        path: str,
        recursive: bool = False,
        max_depth: int | None = None,
        include_sha256: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"path": path, "recursive": recursive, "includeSha256": include_sha256}
        if max_depth is not None:
            body["maxDepth"] = max_depth
        response = self._send(
            "POST",
            f"/v1/sessions/{session_id}/files:list",
            session_id=session_id,
            token=token,
            operation="files:list",
            json=body,
        )
        return _json_object(response, operation="files:list")


__all__ = [
    "CreateSessionResult",
    "ErrorCode",
    "ExecResult",
    "SessionState",
    "SessionStatus",
    "UploadResult",
    "WorkstationAccessDenied",
    "WorkstationApiError",
    "WorkstationClient",
    "WorkstationConfig",
    "WorkstationError",
    "WorkstationProtocolError",
    "WorkstationTransportError",
]
