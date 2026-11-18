"""Smoke tests for system endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/health/liveness")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok", "runtime": "ok"}


@pytest.mark.asyncio
async def test_version(client: AsyncClient) -> None:
    response = await client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert "name" in body
    assert "version" in body


@pytest.mark.asyncio
async def test_readiness_ok(client: AsyncClient) -> None:
    response = await client.get("/health/readiness")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_metrics(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "workstation_service_info" in response.text
    assert "workstation_http_requests_total" in response.text


@pytest.mark.asyncio
async def test_openapi_documents_session_auth_headers(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    operation = spec["paths"]["/v1/sessions/{session_id}/exec"]["post"]

    assert operation["security"] == [{"HTTPBearer": []}]
    x_session_id = next(param for param in operation["parameters"] if param["name"] == "X-Session-Id")
    assert x_session_id["in"] == "header"
    assert x_session_id["required"] is True

    stream_operation = spec["paths"]["/v1/sessions/{session_id}/exec:stream"]["post"]
    assert "application/x-ndjson" in stream_operation["responses"]["200"]["content"]
    assert "application/json" not in stream_operation["responses"]["200"]["content"]

    download_operation = spec["paths"]["/v1/sessions/{session_id}/files:download"]["post"]
    assert "application/octet-stream" in download_operation["responses"]["200"]["content"]
    assert "application/json" not in download_operation["responses"]["200"]["content"]
    assert "X-File-Sha256" in download_operation["responses"]["200"]["headers"]
    assert "X-File-Bytes" in download_operation["responses"]["200"]["headers"]
