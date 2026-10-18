"""Тело вызова инструмента: `{ctx, args}` (CONTRACTS.md §3.0)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from finengine.assistant.turn_record import TurnRecord


class ToolCtx(BaseModel):
    """`ToolContext` ouroboros: кто вызвал и в каком ходе. `run_id` = `transport.source_message_id`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_id: str
    agent_id: str = ""
    chat_id: str
    task_id: str
    root_task_id: str = ""
    task_depth: int = 0
    run_id: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ctx: ToolCtx
    args: dict[str, Any] = {}


@dataclass(frozen=True)
class BoundCall:
    """Вызов, принятый привязкой: запись хода и аргументы."""

    ctx: ToolCtx
    args: dict[str, Any]
    record: TurnRecord

    @property
    def is_root(self) -> bool:
        return self.ctx.task_depth == 0
