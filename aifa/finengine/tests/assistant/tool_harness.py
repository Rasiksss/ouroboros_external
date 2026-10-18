"""Общее для тестов внутреннего API инструментов: ход-прогон, запись хода, HTTP-клиент вместо плагина.

HTTP-клиент играет роль плагина `aifa-engine` (CONTRACTS.md §3.0): тело `{ctx, args}`, заголовок
`Authorization: Bearer`. Прогон создаётся настоящим `run_orchestrator.start_run`; он «жив», пока
тест не отпустит его.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from finengine.assistant import tool_api
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord, remember
from finengine.config import settings
from finengine.skills import run_orchestrator as ro
from finengine.store import bus as B
from finengine.store import cache as C

TOKEN = "test-engine-token"
OWNER = "7"
CHAT = "project-1"
ROOT = "root-task"


@pytest.fixture
def coordination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Журнал, шина и кэш состояния прогонов — во временном каталоге и в памяти; токен задан."""
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "aifa_engine_token", TOKEN)
    B.reset_bus()
    C._cached = None
    yield
    B.reset_bus()
    C._cached = None
    ro._runs.clear()


@dataclass
class OpenRun:
    """Прогон хода, который держится открытым, пока тест не вызовет `end()`."""

    run_id: str
    record: TurnRecord
    _release: asyncio.Event = field(default_factory=asyncio.Event)

    async def end(self) -> None:
        self._release.set()
        state = ro.get_run(self.run_id)
        assert state is not None and state.task is not None
        await asyncio.wait({state.task}, timeout=5)

    async def events(self) -> list[dict[str, Any]]:
        """Журнал хода так, как его реплеит ручка `/chat/runs/{id}/events` (без сервисных кадров)."""
        found: list[dict[str, Any]] = []
        async for event in ro.subscribe(self.run_id, kind="chat"):
            found.append(event)
        return found


@dataclass
class Idle:
    release: asyncio.Event

    async def __call__(self) -> AsyncIterator[dict[str, Any]]:
        await self.release.wait()
        yield {"type": "done"}


def open_run(**record: Any) -> OpenRun:
    release = asyncio.Event()
    run_id = ro.start_run(Idle(release), kind="chat", meta={})
    fields: dict[str, Any] = {
        "run_id": run_id,
        "owner_id": OWNER,
        "chat_id": CHAT,
        "message": "какая выручка за 2024 год?",
        "user_id": int(OWNER),
        "case_id": CHAT,
        "doc_ids": ("doc-1",),
        "active_doc_id": "",
        "role": "",
    }
    fields.update(record)
    turn = TurnRecord(**fields)
    remember(turn)
    return OpenRun(run_id, turn, release)


def ctx_of(
    run_id: str,
    *,
    task_id: str = ROOT,
    root_task_id: str = "",
    task_depth: int = 0,
    owner_id: str = OWNER,
    chat_id: str = CHAT,
) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "agent_id": "main",
        "chat_id": chat_id,
        "task_id": task_id,
        "root_task_id": root_task_id,
        "task_depth": task_depth,
        "run_id": run_id,
    }


def app_of(api: ToolApi) -> FastAPI:
    app = FastAPI()
    app.include_router(tool_api.router)
    app.dependency_overrides[tool_api.get_tool_api] = lambda: api
    return app


class Plugin:
    """Клиент, играющий плагин: `await plugin.call("aifa_goal", ctx)` → `httpx.Response`."""

    def __init__(self, api: ToolApi) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_of(api)), base_url="http://engine"
        )

    async def call(
        self,
        tool: str,
        ctx: dict[str, Any],
        args: dict[str, Any] | None = None,
        *,
        token: str | None = TOKEN,
    ) -> httpx.Response:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return await self._client.post(
            f"/internal/ouro/v1/{tool}", json={"ctx": ctx, "args": args or {}}, headers=headers
        )

    async def close(self) -> None:
        await self._client.aclose()


def first_line(response: httpx.Response) -> str:
    return response.text.split("\n", 1)[0]
