"""Schedule lifecycle tools over ``ouroboros.scheduling``; the caller's scope rides in ctx."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.scheduling.core import RECURRENCES, WEEKDAYS
from ouroboros.scheduling.service import SchedulerService
from ouroboros.scheduling.store import default_schedule_store

_TOOL_TIMEOUT_SEC = 20


def _service() -> SchedulerService:
    return SchedulerService(default_schedule_store())


def _answers_json(handler: Callable[..., dict[str, Any]]) -> Callable[..., str]:
    """A tool answers one JSON string; a refusal is left to raise.

    The provider spells an escaping exception ``⚠️ TOOL_ERROR``, which is what the loop reads as
    a failure. A payload whose own field says "error" is recorded and reflected on as a success.
    """

    @functools.wraps(handler)
    def answer(ctx: ToolContext, **kwargs: Any) -> str:
        return json.dumps(handler(ctx, **kwargs), ensure_ascii=False, sort_keys=True)

    return answer


@_answers_json
def _create_reminder(
    ctx: ToolContext,
    *,
    message: str = "",
    recurrence: str = "",
    time: str | None = None,
    weekday: str | None = None,
    interval_minutes: int | None = None,
    at: str | None = None,
    in_minutes: int | None = None,
    in_hours: int | None = None,
    in_days: int | None = None,
) -> dict[str, Any]:
    return _service().create_reminder(
        ctx,
        message=message,
        recurrence=recurrence,
        time=time,
        weekday=weekday,
        interval_minutes=interval_minutes,
        at=at,
        in_minutes=in_minutes,
        in_hours=in_hours,
        in_days=in_days,
    )


@_answers_json
def _create_scheduled_task(
    ctx: ToolContext,
    *,
    instruction: str = "",
    recurrence: str = "",
    time: str | None = None,
    weekday: str | None = None,
    interval_minutes: int | None = None,
    at: str | None = None,
    in_minutes: int | None = None,
    in_hours: int | None = None,
    in_days: int | None = None,
) -> dict[str, Any]:
    return _service().create_scheduled_task(
        ctx,
        instruction=instruction,
        recurrence=recurrence,
        time=time,
        weekday=weekday,
        interval_minutes=interval_minutes,
        at=at,
        in_minutes=in_minutes,
        in_hours=in_hours,
        in_days=in_days,
    )


@_answers_json
def _edit_schedule(
    ctx: ToolContext,
    *,
    id: str = "",
    revision: int = 0,
    what: str | None = None,
    recurrence: str | None = None,
    time: str | None = None,
    weekday: str | None = None,
    interval_minutes: int | None = None,
    at: str | None = None,
    in_minutes: int | None = None,
    in_hours: int | None = None,
    in_days: int | None = None,
) -> dict[str, Any]:
    return _service().edit_schedule(
        ctx,
        id,
        revision=revision,
        what=what,
        recurrence=recurrence,
        time=time,
        weekday=weekday,
        interval_minutes=interval_minutes,
        at=at,
        in_minutes=in_minutes,
        in_hours=in_hours,
        in_days=in_days,
    )


@_answers_json
def _confirm_schedule(ctx: ToolContext, *, id: str = "", revision: int = 0) -> dict[str, Any]:
    return _service().confirm_schedule(ctx, id, revision=revision)


@_answers_json
def _list_schedules(ctx: ToolContext, *, state: str = "", cursor: int = 0, limit: int = 0) -> dict[str, Any]:
    return _service().list_schedules(ctx.scope, state=state, cursor=cursor, limit=limit)


@_answers_json
def _cancel_schedule(ctx: ToolContext, *, id: str = "", revision: int = 0) -> dict[str, Any]:
    return _service().cancel_schedule(ctx, id, revision=revision)


@_answers_json
def _pause_schedule(ctx: ToolContext, *, id: str = "", revision: int = 0) -> dict[str, Any]:
    return _service().pause_schedule(ctx, id, revision=revision)


@_answers_json
def _resume_schedule(ctx: ToolContext, *, id: str = "", revision: int = 0) -> dict[str, Any]:
    return _service().resume_schedule(ctx, id, revision=revision)


@_answers_json
def _move_schedule_here(ctx: ToolContext, *, id: str = "", revision: int = 0) -> dict[str, Any]:
    return _service().move_schedule_here(ctx, schedule_id=id, revision=revision)


_REVISION_PROPERTY = {
    "type": "integer",
    "minimum": 1,
    "description": "Current revision returned by create/list/edit; stale revisions are rejected.",
}
_ID_PROPERTY = {
    "type": "string",
    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    "description": "Schedule UUID.",
}
_TIME_PROPERTIES = {
    "recurrence": {"type": "string", "enum": list(RECURRENCES)},
    "time": {"type": "string", "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$"},
    "weekday": {"type": "string", "enum": list(WEEKDAYS)},
    "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 525600},
    "at": {
        "type": "string",
        "description": "ISO local datetime; the owner's timezone is applied when no offset is present.",
    },
    "in_minutes": {"type": "integer", "minimum": 1},
    "in_hours": {"type": "integer", "minimum": 1},
    "in_days": {"type": "integer", "minimum": 1},
}


def _mutation_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"id": dict(_ID_PROPERTY), "revision": dict(_REVISION_PROPERTY)},
        "required": ["id", "revision"],
        "additionalProperties": False,
    }


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="create_reminder",
            schema=ToolSchema(
                name="create_reminder",
                description=(
                    "Schedule saved text for direct delivery without an LLM. A one-time reminder activates "
                    "immediately; a recurring reminder remains pending until confirm_schedule. "
                    "`at` and the relative delays imply recurrence=once; recurrence is required only for "
                    "a repeating reminder."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                        **_TIME_PROPERTIES,
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            ),
            handler=_create_reminder,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="create_scheduled_task",
            schema=ToolSchema(
                name="create_scheduled_task",
                description=(
                    "Create a scheduled agent task that will run through the LLM and deliver its result to this "
                    "chat. It always remains pending until confirm_schedule. Make instruction self-contained. "
                    "`at` and the relative delays imply recurrence=once; recurrence is required only for "
                    "a repeating task."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "minLength": 1, "maxLength": 8000},
                        **_TIME_PROPERTIES,
                    },
                    "required": ["instruction"],
                    "additionalProperties": False,
                },
            ),
            handler=_create_scheduled_task,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="edit_schedule",
            schema=ToolSchema(
                name="edit_schedule",
                description=(
                    "Edit a pending or needs-attention schedule. First list schedules and pass its current "
                    "revision. The edited schedule remains pending and needs confirmation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": dict(_ID_PROPERTY),
                        "revision": dict(_REVISION_PROPERTY),
                        "what": {"type": "string", "minLength": 1, "maxLength": 8000},
                        **_TIME_PROPERTIES,
                    },
                    "required": ["id", "revision"],
                    "additionalProperties": False,
                },
            ),
            handler=_edit_schedule,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="confirm_schedule",
            schema=ToolSchema(
                name="confirm_schedule",
                description=(
                    "Activate a pending schedule after the user confirms the read-back. "
                    "Rejects past one-time timestamps."
                ),
                parameters=_mutation_parameters(),
            ),
            handler=_confirm_schedule,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="list_schedules",
            schema=ToolSchema(
                name="list_schedules",
                description=(
                    "One page of this chat's schedules, soonest first, with current revisions and localized "
                    "next runs. A schedule created in another chat is not listed here and cannot be reached "
                    "from here. Call before edit, cancel, pause, resume, confirm, or move. Narrow with state, "
                    "and pass cursor=next_cursor to continue while next_cursor is present."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "enum": ["active", "pending", "paused", "needs_attention", "completed", "cancelled"],
                            "description": "Optional: list only schedules in this state.",
                        },
                        "cursor": {"type": "integer", "minimum": 0, "description": "Zero-based next_cursor."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    },
                    "additionalProperties": False,
                },
            ),
            handler=_list_schedules,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="cancel_schedule",
            schema=ToolSchema(
                name="cancel_schedule",
                description="Permanently stop future runs of a schedule. Pass its current revision.",
                parameters=_mutation_parameters(),
            ),
            handler=_cancel_schedule,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="pause_schedule",
            schema=ToolSchema(
                name="pause_schedule",
                description="Pause an active schedule without deleting it. Pass its current revision.",
                parameters=_mutation_parameters(),
            ),
            handler=_pause_schedule,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="resume_schedule",
            schema=ToolSchema(
                name="resume_schedule",
                description=(
                    "Resume a paused schedule at its next future slot; missed slots are not run. "
                    "Pass its current revision."
                ),
                parameters=_mutation_parameters(),
            ),
            handler=_resume_schedule,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
        ToolEntry(
            name="move_schedule_here",
            schema=ToolSchema(
                name="move_schedule_here",
                description=(
                    "Re-address future delivery of a pending, active, paused, or needs-attention schedule of "
                    "this chat to whoever is asking now. In a group chat that is what points later runs at "
                    "them instead of at the participant who created it. It cannot reach a schedule of another "
                    "chat. Never ask for or pass a raw chat id; list first and pass the current revision."
                ),
                parameters=_mutation_parameters(),
            ),
            handler=_move_schedule_here,
            timeout_sec=_TOOL_TIMEOUT_SEC,
        ),
    ]


__all__ = ["get_tools"]
