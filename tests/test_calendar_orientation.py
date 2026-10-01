from __future__ import annotations

import json
import pathlib

import pytest

from new_core.tools.context import ToolContext
from tests._shared import make_tool_env


@pytest.fixture
def tool_context(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")


def _resolve(tool_context: ToolContext, **kwargs) -> dict:
    from ouroboros.tools.calendar_resolve import _calendar_resolve

    return json.loads(_calendar_resolve(tool_context, **kwargs))


def test_calendar_resolve_weekday_for_date(tool_context):
    result = _resolve(
        tool_context,
        mode="weekday_for_date",
        date="2026-07-01",
    )

    assert result == {
        "date": "2026-07-01",
        "locale": "ru-RU",
        "status": "ok",
        "timezone": "Europe/Moscow",
        "weekday": "среда",
        "weekday_iso": 3,
    }


def test_calendar_resolve_relative_weekday_directions(tool_context):
    next_result = _resolve(
        tool_context,
        mode="date_for_weekday",
        weekday="среда",
        reference_date="2026-07-01",
        direction="next",
    )
    current_result = _resolve(
        tool_context,
        mode="date_for_weekday",
        weekday="среда",
        reference_date="2026-07-01",
        direction="current_or_next",
    )
    previous_result = _resolve(
        tool_context,
        mode="date_for_weekday",
        weekday="среда",
        reference_date="2026-07-01",
        direction="previous",
    )

    assert next_result["date"] == "2026-07-08"
    assert current_result["date"] == "2026-07-01"
    assert previous_result["date"] == "2026-06-24"


def test_calendar_resolve_month_and_year_offsets_clamp_day(tool_context):
    month = _resolve(
        tool_context,
        mode="date_offset",
        reference_date="2026-01-31",
        amount=1,
        unit="months",
    )
    year = _resolve(
        tool_context,
        mode="date_offset",
        reference_date="2024-02-29",
        amount=1,
        unit="years",
    )

    assert month["date"] == "2026-02-28"
    assert year["date"] == "2025-02-28"


def test_calendar_resolve_naive_datetime_uses_selected_timezone(tool_context):
    result = _resolve(
        tool_context,
        mode="date_offset",
        reference_datetime="2026-07-01T23:30:00",
        amount=1,
        unit="days",
        timezone="Asia/Tokyo",
    )

    assert result["date"] == "2026-07-02"
    assert result["timezone"] == "Asia/Tokyo"


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"mode": "weekday_for_date"}, "date is required"),
        (
            {"mode": "date_for_weekday", "weekday": "среда"},
            "reference_date or reference_datetime is required",
        ),
        (
            {
                "mode": "date_for_weekday",
                "weekday": "среда",
                "reference_date": "2026-07-01",
                "direction": "sideways",
            },
            "direction must be",
        ),
        (
            {
                "mode": "date_offset",
                "reference_date": "2026-07-01",
                "unit": "quarters",
            },
            "unit must be",
        ),
        ({"mode": "unknown"}, "mode must be"),
    ],
)
def test_calendar_resolve_reports_validation_errors(tool_context, kwargs, message):
    result = _resolve(tool_context, **kwargs)

    assert result["status"] == "error"
    assert message in result["message"]


def test_runtime_context_includes_validated_local_time(tmp_path, monkeypatch):
    from ouroboros.agent import context as context_module
    from ouroboros.agent.context import build_runtime_section

    class Env:
        repo_dir = tmp_path / "repo"
        drive_root = tmp_path

    monkeypatch.setenv("OUROBOROS_TIMEZONE", "Europe/Moscow")
    monkeypatch.setenv("OUROBOROS_LOCALE", "ru-RU")
    monkeypatch.setattr(
        context_module,
        "utc_now_iso",
        lambda: "2026-06-30T09:00:00+00:00",
    )

    payload = json.loads(build_runtime_section(Env(), {"id": "task-1", "type": "task"}).split("\n\n", 1)[1])

    assert payload["current_local_datetime"].startswith("2026-06-30T12:00:00")
    assert payload["timezone"] == "Europe/Moscow"
    assert payload["locale"] == "ru-RU"
    assert payload["weekday"] == "вторник"


def test_calendar_skill_and_core_tool_are_available(tmp_path):
    from ouroboros.skills.loader import find_skill

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    skill = find_skill(tmp_path, "calendar-orientation", workspace_root=repo_root)
    catalog, _ctx = make_tool_env(repo_root, tmp_path)

    assert skill is not None
    assert skill.manifest.type == "instruction"
    assert catalog.find("calendar_resolve") is not None
    assert "calendar_resolve" in [d.name for d in catalog.descriptors() if d.surface == "core"]
    assert "calendar_resolve" in (repo_root / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
