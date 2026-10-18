"""Внутренний HTTP API инструментов `aifa-engine`: `/internal/ouro/v1/<tool>` (CONTRACTS.md §3.0, ADR-0028).

Роут тонкий: токен, разбор тела, дальше `ToolApi`. Привязка вызова к ходу, роли и задания — доменные
объекты (`Binding`, `JobBoard`); текст ответа — только `ouro_view`.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from finengine.assistant import ouro_view
from finengine.assistant.binding import Binding, Refused, RootSink
from finengine.assistant.goal_tools import GoalSource, GoalTool, WaitTool
from finengine.assistant.jobs import JobBoard
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.tool_call import BoundCall, ToolCall
from finengine.config import settings

log = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class Reply:
    text: str
    status: int = 200


class Tool(Protocol):
    name: str
    root_only: bool

    async def run(self, call: BoundCall) -> str: ...


class ToolApi:
    def __init__(self, gateway: RootSink, goal: GoalSource) -> None:
        board = JobBoard(settings.ouroboros_run_watch_sec)
        self._binding = Binding(gateway)
        self._tools: dict[str, Tool] = {tool.name: tool for tool in (GoalTool(board, goal), WaitTool(board))}

    async def handle(self, tool_name: str, body: bytes) -> Reply:
        tool = self._tools.get(tool_name)
        if tool is None:
            return Reply(ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"Инструмента {tool_name} нет."), 404)
        try:
            call = ToolCall.model_validate_json(body)
        except ValidationError:
            return Reply(ouro_view.refusal(Refusal.BAD_ARGUMENTS, "Тело вызова не по контракту: нужны ctx и args."))
        bound = self._binding.bind(call)
        if isinstance(bound, Refused):
            return Reply(ouro_view.refusal(bound.code, bound.why))
        if tool.root_only and not bound.is_root:
            return Reply(ouro_view.refusal(Refusal.NOT_THE_ROLE, f"{tool.name} вызывает только корень хода."))
        try:
            return Reply(await tool.run(bound))
        except Exception as failure:  # noqa: BLE001 — граница API: системная ошибка → 5xx, не отказ правила
            log.warning("инструмент %s хода %s: системная ошибка %r", tool_name, bound.ctx.run_id[:8], failure)
            return Reply(ouro_view.service_fault(f"{tool_name} не выполнен"), 500)


def get_tool_api() -> ToolApi:
    from finengine.assistant.composition import tool_api  # noqa: PLC0415 — разрыв цикла: composition собирает ToolApi

    return tool_api()


def require_token(authorization: str) -> None:
    """Пустой секрет закрывает API целиком (503), а не открывает (ADR-0028: вызовы меняют `Case`)."""
    token = settings.aifa_engine_token
    if not token:
        raise HTTPException(status_code=503, detail="AIFA_ENGINE_TOKEN не задан: внутренний API закрыт")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="bad token")


@router.post("/internal/ouro/v1/{tool}")
async def call_tool(
    tool: str,
    request: Request,
    authorization: str = Header(""),
    api: ToolApi = Depends(get_tool_api),
) -> Response:
    require_token(authorization)
    reply = await api.handle(tool, await request.body())
    return PlainTextResponse(reply.text, status_code=reply.status)
