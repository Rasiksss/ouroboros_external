"""Helpers to shape the fake auth response in a single test."""

from __future__ import annotations

from typing import Any

from app.core.clients.auth import auth_client


def set_auth_user(monkeypatch, **user_fields: Any) -> None:
    """Override the auth mock response for the current test only."""
    default: dict[str, Any] = {
        "fio": "Test User",
        "roles": [],
        "permissions": ["EXAMPLE_VIEW"],
        "agents": [],
    }
    default.update(user_fields)

    async def fake_get_auth_data(auth_token: str) -> dict[str, Any]:
        return default

    monkeypatch.setattr(auth_client, "get_auth_data", fake_get_auth_data)
