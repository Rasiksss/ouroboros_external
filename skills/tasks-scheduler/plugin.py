"""PluginAPI registration for the autonomous tasks scheduler."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from .scheduler_core import RECURRENCES, WEEKDAYS, ScheduleValidationError
from .scheduler_service import SchedulerError, SchedulerService
from .scheduler_storage import SharedScheduleStore, StorageError

POLL_SECONDS = 20
_service: SchedulerService | None = None
_ALL_SETTING_KEYS = ("OUROBOROS_TIMEZONE",)


def _scheduler() -> SchedulerService:
    if _service is None:
        raise SchedulerError("tasks scheduler is not initialized")
    return _service


def _json_call(action: Callable[[], dict[str, Any]]) -> str:
    try:
        payload = action()
    except (ScheduleValidationError, SchedulerError, StorageError) as exc:
        payload = {"status": "error", "message": str(exc)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _interactive_json_call(ctx: Any, action: Callable[[], dict[str, Any]]) -> str:
    def run() -> dict[str, Any]:
        _scheduler().require_interactive(ctx)
        return action()

    return _json_call(run)


def _create_reminder(
    ctx: Any,
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
) -> str:
    return _json_call(
        lambda: _scheduler().create_reminder(
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
    )


def _create_scheduled_task(
    ctx: Any,
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
) -> str:
    return _json_call(
        lambda: _scheduler().create_scheduled_task(
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
    )


def _edit_schedule(
    ctx: Any,
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
) -> str:
    return _interactive_json_call(
        ctx,
        lambda: _scheduler().edit_schedule(
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
        ),
    )


def _confirm_schedule(ctx: Any, *, id: str = "", revision: int = 0) -> str:
    return _interactive_json_call(
        ctx,
        lambda: _scheduler().confirm_schedule(id, revision=revision),
    )


def _list_schedules(ctx: Any) -> str:
    return _interactive_json_call(ctx, _scheduler().list_schedules)


def _cancel_schedule(ctx: Any, *, id: str = "", revision: int = 0) -> str:
    return _interactive_json_call(
        ctx,
        lambda: _scheduler().cancel_schedule(id, revision=revision),
    )


def _pause_schedule(ctx: Any, *, id: str = "", revision: int = 0) -> str:
    return _interactive_json_call(
        ctx,
        lambda: _scheduler().pause_schedule(id, revision=revision),
    )


def _resume_schedule(ctx: Any, *, id: str = "", revision: int = 0) -> str:
    return _interactive_json_call(
        ctx,
        lambda: _scheduler().resume_schedule(id, revision=revision),
    )


def _move_schedule_here(ctx: Any, *, id: str = "", revision: int = 0) -> str:
    return _json_call(
        lambda: _scheduler().move_schedule_here(
            ctx,
            schedule_id=id,
            revision=revision,
        )
    )


async def _scheduler_loop() -> None:
    while True:
        await asyncio.to_thread(_scheduler().tick)
        await asyncio.sleep(POLL_SECONDS)


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
        "description": "ISO local datetime; runtime timezone is applied when no offset is present.",
    },
    "in_minutes": {"type": "integer", "minimum": 1},
    "in_hours": {"type": "integer", "minimum": 1},
    "in_days": {"type": "integer", "minimum": 1},
}


def _mutation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"id": dict(_ID_PROPERTY), "revision": dict(_REVISION_PROPERTY)},
        "required": ["id", "revision"],
        "additionalProperties": False,
    }


# safety "skip" throughout: these tools manage the agent's own schedule; a
# scheduled task's own tool calls still pass the supervisor when it runs.
def register(api: Any) -> None:
    global _service
    settings = api.get_settings(_ALL_SETTING_KEYS)
    store = SharedScheduleStore(api.get_state_store())
    timezone_name = str(settings.get("OUROBOROS_TIMEZONE") or "Europe/Moscow")
    _service = SchedulerService(store, api, timezone_name=timezone_name)

    api.register_tool(
        "create_reminder",
        _create_reminder,
        description=(
            "Schedule saved text for direct delivery without an LLM. A one-time reminder activates immediately; "
            "a recurring reminder remains pending until confirm_schedule. Relative delays force recurrence=once."
        ),
        schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                **_TIME_PROPERTIES,
            },
            "required": ["message", "recurrence"],
            "additionalProperties": False,
        },
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "create_scheduled_task",
        _create_scheduled_task,
        description=(
            "Create a scheduled agent task that will run through the LLM and deliver its result to this chat. "
            "It always remains pending until confirm_schedule. Make instruction self-contained."
        ),
        schema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "minLength": 1, "maxLength": 8000},
                **_TIME_PROPERTIES,
            },
            "required": ["instruction", "recurrence"],
            "additionalProperties": False,
        },
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "edit_schedule",
        _edit_schedule,
        description=(
            "Edit a pending or needs-attention schedule. First list schedules and pass its current revision. "
            "The edited schedule remains pending and needs confirmation."
        ),
        schema={
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
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "confirm_schedule",
        _confirm_schedule,
        description="Activate a pending schedule after the user confirms the read-back. Rejects past one-time timestamps.",
        schema=_mutation_schema(),
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "list_schedules",
        _list_schedules,
        description=(
            "List schedules grouped by state with current revisions, localized next runs, and delivery chats. "
            "Call before edit, cancel, pause, resume, confirm, or move."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        timeout_sec=20,
        safety_policy="skip",
        capability_class="read_only",
    )
    api.register_tool(
        "cancel_schedule",
        _cancel_schedule,
        description="Permanently stop future runs of a schedule. Pass its current revision.",
        schema=_mutation_schema(),
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "pause_schedule",
        _pause_schedule,
        description="Pause an active schedule without deleting it. Pass its current revision.",
        schema=_mutation_schema(),
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "resume_schedule",
        _resume_schedule,
        description="Resume a paused schedule at its next future slot; missed slots are not run. Pass its current revision.",
        schema=_mutation_schema(),
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_tool(
        "move_schedule_here",
        _move_schedule_here,
        description=(
            "Move future delivery of a pending, active, paused, or needs-attention schedule to the current chat. "
            "Never ask for or pass a raw chat id; list first and pass the current revision."
        ),
        schema=_mutation_schema(),
        timeout_sec=20,
        safety_policy="skip",
        capability_class="mutative",
    )
    api.register_supervised_task("scheduler_loop", _scheduler_loop)
    api.log("info", f"tasks-scheduler registered with {store.backend_name} storage")


__all__ = ["register"]
