"""Infrastructure fixtures for egress tests."""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers.http_double import RecordingHttpClient


@pytest.fixture
def ws_config():
    """Factory for WorkstationConfig with sane defaults; overrides accepted."""
    from ouroboros.egress.workstation.client import WorkstationConfig

    base = {
        "base_url": "https://ws.example",
        "create_secret": "",
        "session_strategy": "ephemeral",
        "idle_timeout_sec": 900,
        "absolute_ttl_sec": 3600,
        "command_timeout_sec": 300,
        "http_timeout_sec": 30.0,
        "max_attempts": 3,
        "retry_base_delay_sec": 0.1,
        "workspace_root": "/workspace",
        "access_level": "rw",
        "attachment_max_bytes": 1024,
    }

    def _make(**overrides: Any) -> WorkstationConfig:
        merged = {**base, **overrides}
        return WorkstationConfig(**merged)

    return _make


@pytest.fixture
def httpx_rig(monkeypatch):
    """Replace httpx.Client with a recording double and patch time.sleep; return instances list."""
    instances: list[RecordingHttpClient] = []

    def _factory(**kwargs: Any) -> RecordingHttpClient:
        client = RecordingHttpClient(**kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr("httpx.Client", _factory)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return instances
