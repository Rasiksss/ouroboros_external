"""Anchor cases for the recurrence core, guarding the verbatim port from the skill."""

from __future__ import annotations

import pytest

from ouroboros.scheduling.core import (
    ScheduleValidationError,
    activate_schedule,
    definition_fingerprint,
    next_occurrence,
    parse_iso_timestamp,
    resume_schedule,
    utc_iso,
)

_NOW = 1_900_000_000.0


def _once(at_ts: float) -> dict:
    return {
        "what": "ping",
        "execution_kind": "reminder",
        "recurrence": "once",
        "timezone": "Europe/Moscow",
        "at": utc_iso(at_ts),
    }


def test_activation_refuses_a_past_one_time_slot():
    with pytest.raises(ScheduleValidationError, match="future"):
        activate_schedule(_once(_NOW - 60), now_ts=_NOW)


def test_activation_stamps_state_and_next_run():
    active = activate_schedule(_once(_NOW + 60), now_ts=_NOW)
    assert active["state"] == "active"
    assert parse_iso_timestamp(active["next_run_at"]) == pytest.approx(_NOW + 60)


def test_an_interval_schedule_needs_its_activation_anchor():
    schedule = {"what": "ping", "recurrence": "interval", "interval_minutes": 30}
    with pytest.raises(ScheduleValidationError, match="anchor"):
        next_occurrence(schedule, after_ts=_NOW)
    schedule["anchor_at"] = utc_iso(_NOW)
    assert next_occurrence(schedule, after_ts=_NOW) == pytest.approx(_NOW + 30 * 60)


def test_resume_skips_missed_slots():
    """Resume schedules the next future occurrence rather than replaying the backlog."""
    schedule = {"what": "ping", "recurrence": "daily", "time": "10:00", "timezone": "Europe/Moscow"}
    resumed = resume_schedule(schedule, now_ts=_NOW)
    assert parse_iso_timestamp(resumed["next_run_at"]) > _NOW


def test_the_fingerprint_ignores_lifecycle_fields_but_not_the_definition():
    base = _once(_NOW + 60)
    decorated = {**base, "state": "pending", "created_at": utc_iso(_NOW), "revision": 3}
    assert definition_fingerprint(base) == definition_fingerprint(decorated)
    assert definition_fingerprint({**base, "what": "pong"}) != definition_fingerprint(base)


def test_weekly_requires_a_known_weekday():
    schedule = {"what": "ping", "recurrence": "weekly", "time": "10:00", "weekday": "пятница"}
    with pytest.raises(ScheduleValidationError, match="weekday"):
        next_occurrence(schedule, after_ts=_NOW)
