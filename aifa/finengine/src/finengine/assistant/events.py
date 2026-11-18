"""События журнала хода, которые пишет клиент хода (CONTRACTS.md §2.5).

Формат — прежний (`run_orchestrator`, экран не меняется): `text_delta`, `text_segment_end`, `done`,
`error`, `session_lost`. Служебные события экран не рисует (`applyChatEvent`: незнакомый тип
пропускается), они нужны журналу и разбору.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from finengine.assistant import messages
from finengine.assistant.tool_names import screen_name

Event = dict[str, Any]
Emit = Callable[[Event], None]  # куда конвейер пишет события хода

SERVICE_EVENT = "ouro_service"


def answer_events(text: str) -> list[Event]:
    """Ответ приходит целиком (ADR-0015): один `text_delta`, граница, конец.

    Конец несёт итог хода: ответ получен — `succeeded`, что бы ни отказал конвейер внутри хода (P62).
    """
    return [{"type": "text_delta", "text": text}, {"type": "text_segment_end"}, {"type": "done", "status": "succeeded"}]


def error_event(message: str) -> Event:
    return {"type": "error", "message": message}


def lost_events() -> list[Event]:
    """`session_lost` и `done`, как у журнала, оборванного перезапуском (`recover_orphan_runs`)."""
    return [{"type": "session_lost", "message": messages.LOST}, {"type": "done"}]


def interrupted_event() -> Event:
    return {"type": "interrupted"}


def service_event(kind: str, **fields: Any) -> Event:
    return {"type": SERVICE_EVENT, "kind": kind, **fields}


def tool_call_event(tool: str) -> Event:
    return {"type": "tool_call", "name": screen_name(tool)}


def tool_output_event(tool: str, ok: bool) -> Event:
    return {"type": "tool_output", "name": screen_name(tool), "ok": ok}
