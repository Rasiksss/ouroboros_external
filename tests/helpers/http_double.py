"""Shared httpx doubles: one response shape, one recording client.

Eleven test files each defined their own ``FakeResponse``/``FakeClient`` with
near-identical interfaces.  This module replaces them with a single pair that
covers every observed usage: ``post``, ``request``, ``stream``, ``close``,
context-manager protocol, queued exceptions, and ``raise_for_status``.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from typing import Any

import httpx


class FakeHttpResponse:
    """Minimal ``httpx.Response`` double.

    Supports every method the inline copies used across the suite:
    ``json()``, ``is_success``, ``raise_for_status()``, ``headers``, ``text``, ``content``.
    """

    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str | None = None,
        content: bytes = b"",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.content = content

        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = _json.dumps(payload)
        else:
            self.text = ""

    @property
    def is_success(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("response has no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://test.example")
            response = httpx.Response(
                self.status_code,
                request=request,
                json=self._payload,
            )
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=request,
                response=response,
            )

    def read(self) -> None:
        return None


class FakeStreamResponse:
    """Minimal streaming response double for ``httpx.Client.stream``."""

    def __init__(
        self,
        status_code: int = 200,
        lines: list[str] | None = None,
        *,
        error_payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self._lines = lines or []
        self._error_payload = error_payload or {"code": "internal", "message": "stream failed"}

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines

    def read(self) -> None:
        return None

    def json(self) -> Any:
        return self._error_payload


class RecordingHttpClient:
    """Stands in for ``httpx.Client``: records calls, returns queued responses.

    Each instance keeps its own ``calls`` and ``responses`` lists — the
    class-level lists in the old inline copies caused cross-test leakage.

    Queue responses (or exceptions to raise) via ``responses`` before the
    code under test makes its call.  If the queue is empty, a default
    ``FakeHttpResponse()`` (200, empty payload) is returned.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.responses: list[FakeHttpResponse | Exception] = []
        self.stream_responses: list[FakeStreamResponse] = []
        self.closed = False

    def __enter__(self) -> RecordingHttpClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next_response()

    def request(self, method: str, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._next_response()

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.stream_responses:
            return self.stream_responses.pop(0)
        return FakeStreamResponse()

    def close(self) -> None:
        self.closed = True

    def _next_response(self) -> FakeHttpResponse:
        if not self.responses:
            return FakeHttpResponse()
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
