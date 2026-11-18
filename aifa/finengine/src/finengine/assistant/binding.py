"""Привязка вызова инструмента к ходу (CONTRACTS.md §3.0, C3.0).

Отказ привязки — значение (`Refused`), не исключение; текст отказа строит `ouro_view`.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

from finengine.assistant.ouro_view import Refusal
from finengine.assistant.tool_call import BoundCall, ToolCall
from finengine.assistant.turn_gateway import ROOT_COMMAND
from finengine.assistant.turn_record import recall, run_is_active, with_root
from finengine.skills import run_orchestrator


class RootSink(Protocol):
    """Шлюз хода (шаг 08): корень назван вызовом инструмента."""

    def root_known(self, run_id: str, task_id: str) -> bool: ...


@dataclass(frozen=True)
class Refused:
    code: Refusal
    why: str


FOREIGN = Refused(Refusal.FOREIGN_RUN, "Этот вызов не относится к ходу, который сейчас идёт.")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Binding:
    gateway: RootSink

    def bind(self, call: ToolCall) -> BoundCall | Refused:
        ctx = call.ctx
        record = recall(ctx.run_id)
        if record is None:
            log.warning("event=aifa_binding_refused reason=unregistered_turn run_id=%s task_id=%s", ctx.run_id, ctx.task_id)
            return Refused(Refusal.FOREIGN_RUN, "Ход не зарегистрирован в AIFA входной интеграцией. Это не признак зависшего предыдущего хода.")
        if not run_is_active(ctx.run_id):
            log.warning("event=aifa_binding_refused reason=inactive_turn run_id=%s task_id=%s", ctx.run_id, ctx.task_id)
            return FOREIGN
        if (ctx.owner_id, ctx.chat_id) != (record.owner_id, record.chat_id):
            return FOREIGN
        if record.agent_id and ctx.agent_id != record.agent_id:
            return FOREIGN
        root = self._root_of(call)
        if root is None:
            return Refused(Refusal.BAD_ARGUMENTS, "Контекст задачи противоречив: глубина и корень не сходятся.")
        if record.root_task_id and record.root_task_id != root:
            return FOREIGN
        if not record.root_task_id:
            record = with_root(record, root)
            self._name_root(ctx.run_id, root)
        return BoundCall(ctx, call.args, record)

    @staticmethod
    def _root_of(call: ToolCall) -> str | None:
        """Корень, к которому относится задача вызова; `None` — `ctx` противоречит сам себе."""
        ctx = call.ctx
        if ctx.task_depth == 0:
            return ctx.task_id if ctx.root_task_id in ("", ctx.task_id) else None
        return ctx.root_task_id if ctx.root_task_id not in ("", ctx.task_id) else None

    def _name_root(self, run_id: str, task_id: str) -> None:
        if not self.gateway.root_known(run_id, task_id):
            # Хода у этой реплики нет (P52): владельцу прогона корень скажет канал управления.
            run_orchestrator.tell_run(run_id, {"cmd": ROOT_COMMAND, "task_id": task_id})
