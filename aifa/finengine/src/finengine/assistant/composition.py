"""Сборка клиента ouroboros для службы: один шлюз хода и один `ToolApi` на процесс (ADR-0022, ADR-0028)."""

from __future__ import annotations

from functools import cache

from finengine.assistant.ouro_client import OuroClient
from finengine.assistant.standard_path import StandardPath
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_gateway import TurnGateway, TurnTimings
from finengine.config import settings


@cache
def ouro_client() -> OuroClient:
    return OuroClient.from_settings(settings)


@cache
def turn_gateway() -> TurnGateway:
    return TurnGateway(ouro_client(), TurnTimings.from_settings(settings))


@cache
def tool_api() -> ToolApi:
    return ToolApi(turn_gateway(), StandardPath())
