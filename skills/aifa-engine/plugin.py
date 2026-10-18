"""Навык aifa-engine: тонкий клиент внутреннего API службы движка (CONTRACTS.md §3.0, ADR-0022).

Здесь только перенос: контекст и аргументы вызова уходят службе, её текст возвращается модели без
изменений. Правил, разбора и форматирования ответа нет — они в службе (`ouro_view`), иначе их
пришлось бы держать в двух местах (F2).
"""
# ruff: noqa: RUF001, RUF002, RUF003

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header

URL_ENV = "AIFA_ENGINE_URL"
TOKEN_ENV = "AIFA_ENGINE_TOKEN"
# ADR-0022 п. 3: служба держит вызов до 240 с (P61); предел инструмента и запроса — с запасом над ним.
TIMEOUT_SEC = 300
RESULT_LIMIT = 12_000
STOP_POLL_SEC = 1.0


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    capability_class: str


TOOLS = (
    Tool(
        name="aifa_goal",
        description=(
            "Выполнить цель пользователя стандартным путём службы: разобрать сообщение, посчитать "
            "показатели по отчёту и вернуть таблицу результатов. Аргументов нет."
        ),
        schema={"type": "object", "properties": {}},
        capability_class="mutative",
    ),
    Tool(
        name="aifa_wait",
        description="Дождаться итога фонового задания службы по job_id из ответа «ЖДИ job=…».",
        schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_sec": {"type": "integer", "minimum": 10, "maximum": 240},
            },
            "required": ["job_id", "wait_sec"],
        },
        capability_class="read_only",
    ),
)


def service_fault(why: str) -> str:
    return f"СБОЙ_СЛУЖБЫ: {why}; повтори вызов"


def context_of(ctx: Any) -> dict[str, Any]:
    """Поля `ToolContext`, которые служба сверяет с ходом (§3.0); `run_id` — ключ хода службы (ADR-0021 п. 2)."""
    return {
        "owner_id": ctx.scope.owner_id,
        "agent_id": ctx.scope.agent_id,
        "chat_id": ctx.scope.chat_id,
        "task_id": ctx.task_id or "",
        "root_task_id": ctx.root_task_id or "",
        "task_depth": ctx.task_depth,
        "run_id": ctx.transport.get("source_message_id", ""),
    }


@dataclass(frozen=True)
class EngineCall:
    """Обработчик одного инструмента: `handler(ctx, **args)` → текст службы."""

    tool: str
    transport: httpx.AsyncBaseTransport | None = None
    stop_poll_sec: float = STOP_POLL_SEC

    async def __call__(self, ctx: Any, **args: Any) -> str:
        url, token = os.environ.get(URL_ENV, ""), os.environ.get(TOKEN_ENV, "")
        missing = [name for name, value in ((URL_ENV, url), (TOKEN_ENV, token)) if not value]
        if missing:
            return service_fault(f"не задано {', '.join(missing)}")
        # Инвариант 6: оба заголовка на каждом запросе, собираются заново, а не кэшируются.
        headers = with_agent_header(
            with_trace_header({"Authorization": f"Bearer {token}"}, ctx.trace_id, generate=True)
        )
        body = {"ctx": context_of(ctx), "args": args}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_SEC) as client:
                request = asyncio.ensure_future(
                    client.post(f"{url.rstrip('/')}/internal/ouro/v1/{self.tool}", json=body, headers=headers)
                )
                return await self._until_answered_or_stopped(request, ctx)
        except httpx.HTTPError as failure:
            return service_fault(f"служба не ответила ({type(failure).__name__})")

    async def _until_answered_or_stopped(self, request: asyncio.Future[httpx.Response], ctx: Any) -> str:
        """Один запрос на весь срок; между опросами — `ctx.stop_requested()` (ADR-0022 п. 3)."""
        try:
            while not request.done():
                if ctx.stop_requested():
                    return service_fault("вызов остановлен вместе с ходом")
                await asyncio.wait({request}, timeout=self.stop_poll_sec)
            return request.result().text
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)


def register(api: Any) -> None:
    for tool in TOOLS:
        api.register_tool(
            tool.name,
            EngineCall(tool.name),
            description=tool.description,
            schema=tool.schema,
            timeout_sec=TIMEOUT_SEC,
            result_limit=RESULT_LIMIT,
            safety_policy="skip",
            capability_class=tool.capability_class,
        )
