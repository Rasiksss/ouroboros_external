"""Shared timezone and locale handling for runtime-facing dates."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, user_settings

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_LOCALE = "ru-RU"

_RU_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


@dataclass(frozen=True)
class RuntimeTimeConfig:
    timezone_name: str
    locale: str
    zone: ZoneInfo


def runtime_time_config(
    timezone_name: str | None = None,
    locale: str | None = None,
) -> RuntimeTimeConfig:
    requested_zone = str(
        timezone_name or user_settings.OUROBOROS_TIMEZONE(SETTINGS_OWNER_ID) or DEFAULT_TIMEZONE
    ).strip()
    requested_locale = str(locale or user_settings.OUROBOROS_LOCALE(SETTINGS_OWNER_ID) or DEFAULT_LOCALE).strip()
    try:
        zone = ZoneInfo(requested_zone)
    except ZoneInfoNotFoundError:
        log.warning("Unknown timezone %r; using %s", requested_zone, DEFAULT_TIMEZONE)
        requested_zone = DEFAULT_TIMEZONE
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    return RuntimeTimeConfig(
        timezone_name=requested_zone,
        locale=requested_locale or DEFAULT_LOCALE,
        zone=zone,
    )


def local_datetime(instant: datetime, config: RuntimeTimeConfig) -> datetime:
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(config.zone)


def parse_local_bound(
    value: str,
    *,
    field_name: str,
    config: RuntimeTimeConfig | None = None,
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    active = config or runtime_time_config()
    try:
        if len(raw) == 10:
            parsed = datetime.combine(date.fromisoformat(raw), time.min, tzinfo=active.zone)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=active.zone)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date or datetime") from exc
    return parsed.astimezone(timezone.utc).isoformat()


def russian_weekday(day: date) -> str:
    return _RU_WEEKDAYS[day.isoweekday() - 1]


__all__ = [
    "DEFAULT_LOCALE",
    "DEFAULT_TIMEZONE",
    "RuntimeTimeConfig",
    "local_datetime",
    "parse_local_bound",
    "runtime_time_config",
    "russian_weekday",
]
