"""Роутер инструментов смонтирован в приложение службы и собирается из настроек (`composition`)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tool_harness import TOKEN, ctx_of

from finengine.assistant import composition
from finengine.config import settings
from finengine.store import bus as B
from finengine.store import cache as C


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from finengine.api import main as m

    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "aifa_engine_token", TOKEN)
    B.reset_bus()
    C._cached = None
    composition.tool_api.cache_clear()
    composition.turn_gateway.cache_clear()
    yield TestClient(m.app)
    composition.tool_api.cache_clear()
    composition.turn_gateway.cache_clear()
    B.reset_bus()
    C._cached = None


def test_the_app_serves_the_tools_behind_the_token(client: TestClient) -> None:
    path = "/internal/ouro/v1/aifa_goal"
    body = {"ctx": ctx_of("no-such-run"), "args": {}}

    bare = client.post(path, json=body)
    served = client.post(path, json=body, headers={"Authorization": f"Bearer {TOKEN}"})

    assert bare.status_code == 401
    assert (served.status_code, served.text.split("\n")[0]) == (200, "ОТКАЗ чужой_ход")


def test_one_gateway_and_one_tool_api_per_process() -> None:
    composition.tool_api.cache_clear()
    composition.turn_gateway.cache_clear()

    assert composition.tool_api() is composition.tool_api()
    assert composition.turn_gateway() is composition.turn_gateway()
    composition.tool_api.cache_clear()
    composition.turn_gateway.cache_clear()


def test_the_token_and_limits_come_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0028: секрет — `AIFA_ENGINE_TOKEN`, то же имя у ouroboros; пределы §3.0–§3.1 — поля `Settings`."""
    from finengine.config import Settings

    monkeypatch.setenv("AIFA_ENGINE_TOKEN", "from-the-environment")

    fresh = Settings()

    assert fresh.aifa_engine_token == "from-the-environment"
    assert (fresh.ouroboros_result_limit, fresh.ouroboros_tool_segment_sec) == (12_000, 20.0)
