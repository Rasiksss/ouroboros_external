"""Внутренний HTTP API инструментов `aifa-engine`: `/internal/ouro/v1/<tool>` (CONTRACTS.md §3.0, ADR-0028).

Роут тонкий: токен, разбор тела, дальше `ToolApi`. Привязка вызова к ходу, роли и задания — доменные
объекты (`Binding`, `JobBoard`); текст ответа — только `ouro_view`.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from finengine.assistant import ouro_view
from finengine.assistant.binding import Binding, Refused, RootSink
from finengine.assistant.events import tool_call_event, tool_output_event
from finengine.assistant.atom_tools import RegisterTool, ReportTool
from finengine.assistant.atoms_tool import AtomsTool
from finengine.assistant.goal_tools import OpenTool, Opening, RunEmitter, WaitTool
from finengine.assistant.jobs import JobBoard
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_tools import FinishTool, MoveTool
from finengine.assistant.ouro_view import Code, Refusal
from finengine.assistant.read_tools import DocTool, StateTool
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_call import BoundCall, ToolCall
from finengine.config import settings

log = logging.getLogger(__name__)

router = APIRouter()

# Ответ с такого кода — инструмент не сделал дела (для экрана `tool_output.ok == false`); `ЖДИ` — исход.
FAILED_CODES = (Code.REFUSE, Code.SERVICE_FAULT)


@dataclass(frozen=True)
class Reply:
    text: str
    status: int = 200


class Tool(Protocol):
    name: str
    root_only: bool

    async def run(self, call: BoundCall) -> str: ...


class ToolApi:
    def __init__(
        self, gateway: RootSink, opening: Opening, desk: LoopDesk | None = None, subtasks: SubtaskWaits | None = None,
    ) -> None:
        board = JobBoard(settings.ouroboros_run_watch_sec)
        desk = desk or LoopDesk()
        self._subtasks = subtasks or SubtaskWaits()
        self._binding = Binding(gateway)
        tools: tuple[Tool, ...] = (
            OpenTool(board, opening, desk), WaitTool(board, self._subtasks, desk), StateTool(board, desk), DocTool(board, desk),
            AtomsTool(desk),
            MoveTool(board, desk), RegisterTool(board, desk), ReportTool(desk, self._subtasks),
            FinishTool(board, desk, self._subtasks),
        )
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools}

    def tool_names(self) -> list[str]:
        return list(self._tools)

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
        if not bound.is_root:
            # Подзадачу хода служба запоминает по первому её вызову (ADR-0041 п. 3): навыка она не знает.
            self._subtasks.remember(bound.ctx.run_id, bound.ctx.task_id)
        if tool.root_only and not bound.is_root:
            return Reply(ouro_view.refusal(Refusal.NOT_THE_ROLE, f"{tool.name} вызывает только корень хода."))
        emit = RunEmitter(bound.ctx.run_id)
        emit(tool_call_event(tool_name))
        reply = await self._run(tool, bound)
        emit(tool_output_event(tool_name, ok=not reply.text.startswith(FAILED_CODES)))
        return reply

    async def _run(self, tool: Tool, bound: BoundCall) -> Reply:
        try:
            return Reply(await tool.run(bound))
        except Exception as failure:  # noqa: BLE001 — граница API: системная ошибка → 5xx, не отказ правила
            log.warning("инструмент %s хода %s: системная ошибка %r", tool.name, bound.ctx.run_id[:8], failure)
            return Reply(ouro_view.service_fault(f"{tool.name} не выполнен"), 500)


def get_tool_api() -> ToolApi:
    from finengine.assistant.composition import tool_api  # noqa: PLC0415 — разрыв цикла: composition собирает ToolApi

    return tool_api()


def token_refusal(authorization: str) -> Reply | None:
    """Пустой секрет закрывает API целиком (503), а не открывает (ADR-0028: вызовы меняют `Case`).

    Ответ — в грамматике §3.0, как у остальных: плагин отдаёт модели текст ответа без разбора.
    """
    token = settings.aifa_engine_token
    if not token:
        log.error("AIFA_ENGINE_TOKEN не задан: внутренний API инструментов закрыт")
        return Reply(ouro_view.service_fault("внутренний API службы закрыт: токен не задан, сообщи администратору"), 503)
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        return Reply(ouro_view.service_fault("токен службы не принят, сообщи администратору"), 401)
    return None


@router.post("/internal/ouro/v1/{tool}")
async def call_tool(
    tool: str,
    request: Request,
    authorization: str = Header(""),
    api: ToolApi = Depends(get_tool_api),
) -> Response:
    reply = token_refusal(authorization) or await api.handle(tool, await request.body())
    return PlainTextResponse(reply.text, status_code=reply.status)
