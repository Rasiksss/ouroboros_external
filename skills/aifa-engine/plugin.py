"""Навык aifa-engine: тонкий клиент внутреннего API службы движка (CONTRACTS.md §3.0, ADR-0022 п. 2).

Только перенос; правила, разбор и форматирование ответа — в службе (`ouro_view`, F2).
"""
# ruff: noqa: RUF001, RUF002, RUF003

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, TypedDict

import httpx

from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header

URL_ENV = "AIFA_ENGINE_URL"
TOKEN_ENV = "AIFA_ENGINE_TOKEN"
# ADR-0047 п. 4: служба держит вызов до 240 с; предел инструмента и запроса — с запасом над ним.
TIMEOUT_SEC = 300
EXECUTION_TIMEOUT_SEC = 5400
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
        name="aifa_open",
        description=(
            "Открыть цикл по отчёту хода и получить состояние: профиль, показатели, атомы и артефакты. Аргументов нет."
        ),
        schema={"type": "object", "properties": {}},
        capability_class="mutative",
    ),
    Tool(
        name="aifa_wait",
        description=(
            "Дождаться итога фонового задания службы (job_id из ответа «ЖДИ job=…») либо подзадачи (task_id "
            "из schedule_task); укажи что-то одно."
        ),
        schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "task_id": {"type": "string"},
                "wait_sec": {"type": "integer", "minimum": 10, "maximum": 240},
            },
            "required": ["wait_sec"],
        },
        capability_class="read_only",
    ),
    Tool(
        name="aifa_state",
        description="Состояние цикла. artifact_index — страницы id с фильтром output_type; artifact_ids — содержимое; view=compact — значения и формулы без полной трассы.",
        schema={
            "type": "object",
            "properties": {
                "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "view": {"type": "string", "enum": ["full", "compact"]},
                "artifact_index": {
                    "type": "object",
                    "properties": {
                        "output_type": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "additionalProperties": False,
                },
            },
        },
        capability_class="read_only",
    ),
    Tool(
        name="aifa_doc",
        description="Документы хода или распознанный текст документа по страницам либо поиском.",
        schema={
            "type": "object",
            "properties": {"doc_id": {"type": "string"}, "pages": {"type": "string"}, "query": {"type": "string"}},
        },
        capability_class="read_only",
    ),
    Tool(
        name="aifa_atoms",
        description=(
            "Каталог атомов. list — карточки без кода; get — контракт, версия и исходный код одного атома; "
            "offset — продолжение длинного ответа."
        ),
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get"]},
                "name": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["action"],
        },
        capability_class="read_only",
    ),
    Tool(
        name="aifa_move",
        description="Ход корня: сценарии службы по коду в goals, шаги атомов в steps; атомы-загрузки — в new_atoms.",
        schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "reason": {"type": "string"},
                "plan": {"type": "array", "items": {"type": "string"}},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "parts": {"type": "integer"},
                            "formulas": {"type": "boolean"},
                        },
                        "required": ["kind"],
                    },
                },
                "goals": {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "ref": {"type": "string"},
                                    "scenario": {
                                        "type": "string",
                                        "enum": [
                                            "extract_registered_metric",
                                            "extract_ad_hoc_metric",
                                            "calculate_registered_metric",
                                            "calculate_ad_hoc_metric",
                                            "form",
                                            "full_analysis",
                                            "monitor_changes",
                                            "explain_change",
                                        ],
                                    },
                                    "concepts": {"type": "array", "items": {"type": "string"}},
                                    "period": {"type": "string"},
                                    "role": {"type": "string"},
                                    "depends_on": {"type": "array", "items": {"type": "string"}},
                                    "metric": {"type": "object"},
                                    "formula": {"type": "object"},
                                },
                                "required": ["ref", "scenario"],
                            },
                        ],
                    },
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "atom": {"type": "string"},
                            "concept": {"type": "string"},
                            "period_ref": {"type": "string"},
                            "extra": {"type": "object"},
                        },
                        "required": ["atom"],
                    },
                },
                "new_atoms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "requires": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"input_type": {"type": "string"}, "concept": {"type": "string"}},
                                    "required": ["input_type"],
                                },
                            },
                            "output_type": {"type": "string"},
                            "output_schema": {"type": "object"},
                            "url": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["objective", "reason"],
        },
        capability_class="mutative",
    ),
    Tool(
        name="aifa_register_atom",
        description=(
            "Сдать временный атом с кодом: служба проверит контракт, прогонит код в комнате на данных Case "
            "и зарегистрирует eph.<имя>@<версия>."
        ),
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "purpose": {"type": "string"},
                "requires": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"input_type": {"type": "string"}, "concept": {"type": "string"}},
                        "required": ["input_type"],
                    },
                },
                "output_type": {"type": "string"},
                "output_schema": {"type": "object"},
                "code": {"type": "string"},
            },
            "required": ["name", "purpose", "requires", "output_type", "output_schema", "code"],
        },
        capability_class="mutative",
    ),
    Tool(
        name="aifa_report",
        description="Отчёт подзадачи: ok или error — попытка в ленту хода; done — итог подзадачи (note).",
        schema={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "status": {"type": "string", "enum": ["ok", "error", "done"]},
                "note": {"type": "string"},
                "code": {"type": "string"},
            },
            "required": ["label", "status"],
        },
        capability_class="mutative",
    ),
    Tool(
        name="aifa_finish",
        description="Закрыть цикл: finalize или give_up, опубликовать файлы; только корень.",
        schema={
            "type": "object",
            "x-task-plan-operation": "close",
            "properties": {
                "action": {"type": "string", "enum": ["finalize", "give_up"]},
                "deliverables": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        },
        capability_class="mutative",
    ),
)


def service_fault(why: str) -> str:
    return f"СБОЙ_СЛУЖБЫ: {why}; повтори вызов"


class EngineContext(TypedDict):
    """Поля `ToolContext`, которые служба сверяет с ходом (§3.0); `run_id` — ключ хода службы (ADR-0021 п. 2)."""

    owner_id: str
    agent_id: str
    chat_id: str
    task_id: str
    root_task_id: str
    task_depth: int
    run_id: str


def context_of(ctx: Any) -> EngineContext:
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
                tool = self.tool
                while True:
                    request = asyncio.ensure_future(
                        client.post(f"{url.rstrip('/')}/internal/ouro/v1/{tool}", json=body, headers=headers)
                    )
                    text = await self._until_answered_or_stopped(request, ctx)
                    headline = text.split("\n", 1)[0]
                    if (
                        tool == "aifa_wait"
                        and getattr(ctx, "plan_call_id", "")
                        and headline.startswith(("ОТКАЗ", "СБОЙ_СЛУЖБЫ"))
                    ):
                        raise asyncio.CancelledError("background execution outcome is unavailable")
                    if not getattr(ctx, "plan_call_id", "") or not headline.startswith("ЖДИ job="):
                        return text
                    job_id = headline.removeprefix("ЖДИ job=").strip()
                    if not job_id:
                        raise asyncio.CancelledError("background execution identity is unavailable")
                    tool = "aifa_wait"
                    body = {"ctx": context_of(ctx), "args": {"job_id": job_id, "wait_sec": 240}}
        except httpx.HTTPError as failure:
            if getattr(ctx, "plan_call_id", ""):
                raise asyncio.CancelledError("execution outcome could not be confirmed") from failure
            return service_fault(f"служба не ответила ({type(failure).__name__})")

    async def _until_answered_or_stopped(self, request: asyncio.Future[httpx.Response], ctx: Any) -> str:
        """Один запрос на весь срок; между опросами — `ctx.stop_requested()` (ADR-0047 п. 4)."""
        try:
            while not request.done():
                if ctx.stop_requested():
                    if getattr(ctx, "plan_call_id", ""):
                        raise asyncio.CancelledError("execution wait stopped; body outcome is unconfirmed")
                    return service_fault("вызов остановлен вместе с ходом")
                await asyncio.wait({request}, timeout=self.stop_poll_sec)
            response = request.result()
            if getattr(ctx, "plan_call_id", ""):
                response.raise_for_status()
            return response.text
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
            timeout_sec=EXECUTION_TIMEOUT_SEC if tool.name in {"aifa_move", "aifa_finish"} else TIMEOUT_SEC,
            result_limit=RESULT_LIMIT,
            safety_policy="skip",
            capability_class=tool.capability_class,
        )
