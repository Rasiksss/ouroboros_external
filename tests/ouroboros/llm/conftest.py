"""Infrastructure fixtures for LLM provider tests."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def openai_compat_fake_client():
    """An OpenAI SDK-shaped fake: client.chat.completions.create(**kwargs) -> response with model_dump().
    Returns (fake_client, seen_kwargs_dict)."""
    from tests.helpers.factories import make_openai_compat_fake

    seen: dict[str, Any] = {}
    return make_openai_compat_fake(seen=seen), seen


@pytest.fixture
def fake_httpx_client(monkeypatch):
    """Replace httpx.Client with a recording double; return the instances list."""
    from tests.helpers.http_double import RecordingHttpClient

    instances: list[RecordingHttpClient] = []

    def _factory(**kwargs: Any) -> RecordingHttpClient:
        client = RecordingHttpClient(**kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr("httpx.Client", _factory)
    return instances
