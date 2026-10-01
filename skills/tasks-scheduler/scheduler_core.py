"""Pure schedule validation and occurrence calculation."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RECURRENCES = ("once", "interval", "hourly", "daily", "weekly")
EXECUTION_KINDS = ("reminder", "agent_task")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAYS)}
_UTC = dt.timezone.utc


class ScheduleValidationError(ValueError):
    """A schedule definition is incomplete or unsafe to activate."""


def utc_iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(float(timestamp), _UTC).isoformat()


def parse_iso_timestamp(value: Any, *, timezone_name: str | None = None) -> float:
    raw = str(value or "").strip()
    if not raw:
        raise ScheduleValidationError("datetime is required")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleValidationError(f"invalid ISO datetime: {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timezone_name or ""))
    return parsed.timestamp()


def timezone(name: str) -> ZoneInfo:
    normalized = str(name or "").strip() or "Europe/Moscow"
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleValidationError(f"unknown IANA timezone: {normalized!r}") from exc


def parse_clock(value: Any) -> tuple[int, int]:
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError("time must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleValidationError("time must use HH:MM")
    return hour, minute


def normalize_definition(schedule: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schedule)
    execution_kind = str(normalized.get("execution_kind") or "agent_task").strip().lower()
    normalized["execution_kind"] = execution_kind
    recurrence = str(normalized.get("recurrence") or "").strip().lower()
    normalized["recurrence"] = recurrence
    for field in ("at", "interval_minutes", "time", "weekday"):
        if field == "at" and recurrence == "once":
            continue
        if field == "interval_minutes" and recurrence == "interval":
            continue
        if field == "time" and recurrence in {"daily", "weekly"}:
            continue
        if field == "weekday" and recurrence == "weekly":
            continue
        normalized.pop(field, None)
    if recurrence == "weekly":
        normalized["weekday"] = str(normalized.get("weekday") or "").strip().lower()[:3]
    return normalized


def validate_definition(
    schedule: dict[str, Any],
    *,
    now_ts: float | None = None,
    require_future_once: bool = False,
) -> dict[str, Any]:
    normalized = normalize_definition(schedule)
    what = str(normalized.get("what") or "").strip()
    if not what:
        raise ScheduleValidationError("what is required")
    if len(what) > 8_000:
        raise ScheduleValidationError("what must be at most 8000 characters")
    if what.startswith("/"):
        raise ScheduleValidationError("what must not start with a slash command")
    normalized["what"] = what
    if normalized["execution_kind"] not in EXECUTION_KINDS:
        raise ScheduleValidationError(f"execution_kind must be one of {list(EXECUTION_KINDS)}")

    recurrence = normalized.get("recurrence")
    if recurrence not in RECURRENCES:
        raise ScheduleValidationError(f"recurrence must be one of {list(RECURRENCES)}")
    tz_name = str(normalized.get("timezone") or "").strip() or "Europe/Moscow"
    timezone(tz_name)
    normalized["timezone"] = tz_name

    if recurrence == "once":
        at_ts = parse_iso_timestamp(normalized.get("at"), timezone_name=tz_name)
        if require_future_once and now_ts is not None and at_ts <= float(now_ts):
            raise ScheduleValidationError("once.at must be a future datetime")
        normalized["at"] = utc_iso(at_ts)
    elif recurrence == "interval":
        try:
            interval = int(normalized.get("interval_minutes"))
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("interval_minutes must be an integer") from exc
        if not 1 <= interval <= 525_600:
            raise ScheduleValidationError("interval_minutes must be between 1 and 525600")
        normalized["interval_minutes"] = interval
    elif recurrence in {"daily", "weekly"}:
        hour, minute = parse_clock(normalized.get("time"))
        normalized["time"] = f"{hour:02d}:{minute:02d}"
        if recurrence == "weekly":
            weekday = str(normalized.get("weekday") or "").strip().lower()[:3]
            if weekday not in WEEKDAYS:
                raise ScheduleValidationError(f"weekday must be one of {list(WEEKDAYS)}")
            normalized["weekday"] = weekday
    return normalized


def _valid_wall_time(day: dt.date, hour: int, minute: int, zone: ZoneInfo) -> dt.datetime | None:
    candidate = dt.datetime.combine(day, dt.time(hour=hour, minute=minute), tzinfo=zone)
    round_trip = candidate.astimezone(_UTC).astimezone(zone)
    if round_trip.date() != day or round_trip.hour != hour or round_trip.minute != minute:
        return None
    return candidate


def next_occurrence(schedule: dict[str, Any], *, after_ts: float) -> float | None:
    normalized = validate_definition(schedule)
    recurrence = normalized["recurrence"]
    zone = timezone(normalized["timezone"])
    after = float(after_ts)

    if recurrence == "once":
        candidate = parse_iso_timestamp(normalized["at"], timezone_name=normalized["timezone"])
        return candidate if candidate > after else None

    if recurrence == "interval":
        anchor_raw = normalized.get("anchor_at") or normalized.get("activated_at")
        if not anchor_raw:
            raise ScheduleValidationError("interval schedule has no activation anchor")
        anchor = parse_iso_timestamp(anchor_raw)
        period = int(normalized["interval_minutes"]) * 60
        steps = max(1, math.floor((after - anchor) / period) + 1)
        return anchor + steps * period

    local_after = dt.datetime.fromtimestamp(after, zone)
    if recurrence == "hourly":
        candidate = local_after.replace(minute=0, second=0, microsecond=0)
        if candidate.timestamp() <= after:
            candidate += dt.timedelta(hours=1)
        return candidate.timestamp()

    hour, minute = parse_clock(normalized["time"])
    target_weekday = _WEEKDAY_INDEX.get(str(normalized.get("weekday") or ""))
    for offset in range(370):
        day = local_after.date() + dt.timedelta(days=offset)
        if recurrence == "weekly" and day.weekday() != target_weekday:
            continue
        candidate = _valid_wall_time(day, hour, minute, zone)
        if candidate is not None and candidate.timestamp() > after:
            return candidate.timestamp()
    raise ScheduleValidationError("could not resolve a future occurrence")


def activate_schedule(schedule: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    activated = validate_definition(schedule, now_ts=now_ts, require_future_once=True)
    activated["state"] = "active"
    activated["activated_at"] = utc_iso(now_ts)
    if activated["recurrence"] == "interval":
        activated["anchor_at"] = utc_iso(now_ts)
    next_run = next_occurrence(activated, after_ts=now_ts)
    if next_run is None:
        raise ScheduleValidationError("schedule has no future occurrence")
    activated["next_run_at"] = utc_iso(next_run)
    activated["updated_at"] = utc_iso(now_ts)
    return activated


def resume_schedule(schedule: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
    resumed = validate_definition(schedule)
    resumed["state"] = "active"
    resumed["resumed_at"] = utc_iso(now_ts)
    next_run = next_occurrence(resumed, after_ts=now_ts)
    if next_run is None:
        raise ScheduleValidationError("schedule has no future occurrence")
    resumed["next_run_at"] = utc_iso(next_run)
    resumed["updated_at"] = utc_iso(now_ts)
    return resumed


def display_time(value: Any, timezone_name: str) -> str:
    timestamp = parse_iso_timestamp(value)
    local = dt.datetime.fromtimestamp(timestamp, timezone(timezone_name))
    return f"{local.strftime('%Y-%m-%d %H:%M')} ({timezone_name})"


def schedule_summary(schedule: dict[str, Any]) -> str:
    recurrence = str(schedule.get("recurrence") or "")
    tz_name = str(schedule.get("timezone") or "Europe/Moscow")
    if recurrence == "once":
        return f"once at {display_time(schedule.get('at'), tz_name)}"
    if recurrence == "interval":
        return f"every {schedule.get('interval_minutes')} min"
    if recurrence == "hourly":
        return "hourly at the top of the hour"
    if recurrence == "daily":
        return f"daily at {schedule.get('time')} ({tz_name})"
    if recurrence == "weekly":
        return f"weekly {schedule.get('weekday')} at {schedule.get('time')} ({tz_name})"
    return recurrence


def definition_fingerprint(schedule: dict[str, Any]) -> str:
    normalized = validate_definition(schedule)
    fields = {
        key: normalized.get(key)
        for key in (
            "execution_kind",
            "what",
            "recurrence",
            "timezone",
            "at",
            "interval_minutes",
            "time",
            "weekday",
            "delivery",
        )
        if normalized.get(key) is not None
    }
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "EXECUTION_KINDS",
    "RECURRENCES",
    "WEEKDAYS",
    "ScheduleValidationError",
    "activate_schedule",
    "definition_fingerprint",
    "display_time",
    "next_occurrence",
    "normalize_definition",
    "parse_iso_timestamp",
    "resume_schedule",
    "schedule_summary",
    "utc_iso",
    "validate_definition",
]
