"""Deterministic date and weekday resolution."""
# ruff: noqa: RUF001

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Literal

from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from ouroboros.runtime_time import (
    RuntimeTimeConfig,
    runtime_time_config,
    russian_weekday,
)
from ouroboros.tools.spec import spec_from_callable, tool

CalendarMode = Literal["weekday_for_date", "date_for_weekday", "date_offset"]

_WEEKDAY_ALIASES = {
    "понедельник": 1,
    "пн": 1,
    "вторник": 2,
    "вт": 2,
    "среда": 3,
    "среду": 3,
    "ср": 3,
    "четверг": 4,
    "чт": 4,
    "пятница": 5,
    "пятницу": 5,
    "пт": 5,
    "суббота": 6,
    "субботу": 6,
    "сб": 6,
    "воскресенье": 7,
    "вс": 7,
}
_DIRECTIONS = frozenset({"next", "previous", "current_or_next"})
_UNITS = frozenset({"days", "weeks", "months", "years"})
_MODES = frozenset({"weekday_for_date", "date_for_weekday", "date_offset"})

_SCHEMA = {
    "name": "calendar_resolve",
    "description": (
        "Resolve dates and weekdays deterministically. Use it for relative dates, weekday checks, and calendar offsets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["weekday_for_date", "date_for_weekday", "date_offset"],
            },
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "weekday": {"type": "string", "description": "Russian weekday name or ISO number 1-7."},
            "reference_date": {"type": "string", "description": "ISO anchor date YYYY-MM-DD."},
            "reference_datetime": {"type": "string", "description": "ISO anchor datetime."},
            "amount": {"type": "integer", "default": 0},
            "unit": {
                "type": "string",
                "enum": ["days", "weeks", "months", "years"],
                "default": "days",
            },
            "direction": {
                "type": "string",
                "enum": ["next", "previous", "current_or_next"],
                "default": "next",
            },
            "timezone": {"type": "string", "description": "IANA timezone; runtime default when omitted."},
            "locale": {"type": "string", "description": "Output locale; runtime default when omitted."},
        },
        "required": ["mode"],
        "additionalProperties": False,
    },
}


def _required_date(value: str, field_name: str = "date") -> date:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    return date.fromisoformat(raw)


def _anchor_date(
    reference_date: str,
    reference_datetime: str,
    config: RuntimeTimeConfig,
) -> date:
    if str(reference_date or "").strip():
        return date.fromisoformat(str(reference_date).strip())
    raw = str(reference_datetime or "").strip()
    if not raw:
        raise ValueError("reference_date or reference_datetime is required")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.zone)
    return parsed.astimezone(config.zone).date()


def _weekday_number(value: str) -> int:
    normalized = " ".join(str(value or "").strip().casefold().replace(".", "").split())
    if normalized.isdigit() and 1 <= int(normalized) <= 7:
        return int(normalized)
    weekday = _WEEKDAY_ALIASES.get(normalized)
    if weekday is None:
        raise ValueError(f"unknown weekday: {value}")
    return weekday


def _shift_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _shift_date(day: date, amount: int, unit: str) -> date:
    normalized = str(unit or "days").strip().casefold()
    if normalized not in _UNITS:
        raise ValueError("unit must be days, weeks, months, or years")
    if normalized == "days":
        return day + timedelta(days=amount)
    if normalized == "weeks":
        return day + timedelta(weeks=amount)
    return _shift_months(day, amount * (12 if normalized == "years" else 1))


def _weekday_relative(day: date, target: int, direction: str) -> date:
    normalized = str(direction or "next").strip().casefold()
    if normalized not in _DIRECTIONS:
        raise ValueError("direction must be next, previous, or current_or_next")
    if normalized == "previous":
        return day - timedelta(days=(day.isoweekday() - target) % 7 or 7)
    delta = (target - day.isoweekday()) % 7
    if normalized == "next":
        delta = delta or 7
    return day + timedelta(days=delta)


def _calendar_payload(day: date, config: RuntimeTimeConfig) -> dict[str, Any]:
    return {
        "status": "ok",
        "date": day.isoformat(),
        "weekday_iso": day.isoweekday(),
        "weekday": russian_weekday(day),
        "timezone": config.timezone_name,
        "locale": config.locale,
    }


def resolve_calendar(
    *,
    scope: OwnerScope,
    mode: str,
    date_value: str = "",
    weekday: str = "",
    reference_date: str = "",
    reference_datetime: str = "",
    amount: int = 0,
    unit: str = "days",
    direction: str = "next",
    timezone_name: str = "",
    locale: str = "",
) -> dict[str, Any]:
    normalized_mode = str(mode or "").strip()
    if normalized_mode not in _MODES:
        raise ValueError("mode must be weekday_for_date, date_for_weekday, or date_offset")
    config = runtime_time_config(scope, timezone_name or None, locale or None)
    if normalized_mode == "weekday_for_date":
        return _calendar_payload(_required_date(date_value), config)
    anchor = _anchor_date(reference_date, reference_datetime, config)
    if normalized_mode == "date_for_weekday":
        if not str(weekday or "").strip():
            raise ValueError("weekday is required")
        return _calendar_payload(
            _weekday_relative(anchor, _weekday_number(weekday), direction),
            config,
        )
    return _calendar_payload(_shift_date(anchor, int(amount or 0), unit), config)


@tool(
    name="calendar_resolve",
    schema=_SCHEMA,
    category="task",
    surface="core",
    safety_policy="skip",
    parallel=True,
    timeout_sec=10,
    capability_class="read_only",
)
def _calendar_resolve(
    ctx: ToolContext,
    mode: str = "",
    date: str = "",
    weekday: str = "",
    reference_date: str = "",
    reference_datetime: str = "",
    amount: int = 0,
    unit: str = "days",
    direction: str = "next",
    timezone: str = "",
    locale: str = "",
) -> str:
    try:
        payload = resolve_calendar(
            scope=ctx.scope,
            mode=str(mode or "").strip(),
            date_value=date,
            weekday=weekday,
            reference_date=reference_date,
            reference_datetime=reference_datetime,
            amount=amount,
            unit=unit,
            direction=direction,
            timezone_name=timezone,
            locale=locale,
        )
    except (OverflowError, ValueError) as exc:
        payload = {"status": "error", "message": str(exc)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def get_tools():
    return [spec_from_callable(_calendar_resolve)]


__all__ = ["_calendar_resolve", "get_tools", "resolve_calendar"]
