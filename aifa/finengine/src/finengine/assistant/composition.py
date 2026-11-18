"""Сборка клиента ouroboros для службы: один шлюз хода и один `ToolApi` на процесс (ADR-0022, ADR-0028)."""

from __future__ import annotations

from functools import cache

from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.ouro_client import OuroClient
from finengine.assistant.loop_opening import LoopOpening
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_gateway import TurnGateway, TurnTimings
from finengine.config import settings


@cache
def ouro_client() -> OuroClient:
    return OuroClient.from_settings(settings)


@cache
def subtask_waits() -> SubtaskWaits:
    return SubtaskWaits()


@cache
def turn_gateway() -> TurnGateway:
    return TurnGateway(ouro_client(), TurnTimings.from_settings(settings), subtask_waits())


@cache
def loop_desk() -> LoopDesk:
    return LoopDesk()


@cache
def tool_api() -> ToolApi:
    return ToolApi(turn_gateway(), LoopOpening(loop_desk()), loop_desk(), subtask_waits())
