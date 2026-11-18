"""HTTP request logging middleware tests."""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient


async def test_request_logging_adds_request_id_and_completion_diagnostics(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.core.middlewares")

    response = await client.get("/version", headers={"X-Request-Id": "test-request-id"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "test-request-id"
    start_record = next(record for record in caplog.records if record.message == "http.request start")
    end_record = next(record for record in caplog.records if record.message == "http.request end")
    assert start_record.diagnostics["requestId"] == "test-request-id"
    assert start_record.diagnostics["path"] == "/version"
    assert end_record.diagnostics["requestId"] == "test-request-id"
    assert end_record.diagnostics["statusCode"] == 200
    assert isinstance(end_record.diagnostics["durationMs"], int)
