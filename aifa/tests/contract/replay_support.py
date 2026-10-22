"""Реплей решений планировщика через ouroboros (шаг 14): решение → вызов инструмента → сценарий.

Решение записано в форме ответа модели службы (`live_run_replays.json`): `execute` → `aifa_move`,
`finalize`/`give_up` → `aifa_finish`. Поля со значением `null` в вызов не попадают: навык велит не
передавать то, чего ход не касается.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from stand import SCENARIOS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
from golden_normalize import normalize_turn  # noqa: E402

__all__ = ["normalize_turn"]

SKILL_DIR = SCENARIOS.parents[3] / "skills" / "aifa-planning"

TOOL = "ext_13_r_aifa-engine_{name}"
SKILL = "distribution:aifa-planning"
MOVE_FIELDS = ("objective", "reason", "plan", "files", "goals", "steps", "new_atoms")
FINISH_FIELDS = ("action", "deliverables", "evidence_ids")


def replays() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(
        (SCENARIOS / "planning_replays.json").read_text(encoding="utf-8")
    )
    return loaded


def arguments_of(decision: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Имя инструмента и аргументы для решения планировщика: только заполненные поля."""
    fields = MOVE_FIELDS if decision["action"] == "execute" else FINISH_FIELDS
    name = "aifa_move" if decision["action"] == "execute" else "aifa_finish"
    args = {field: without_nulls(decision[field]) for field in fields if field in decision}
    if name == "aifa_move" and decision.get("scenarios"):
        args["goals"] = [
            *args.get("goals", []),
            *[
                {
                    "ref": f"legacy{index}",
                    "scenario": "extract_ad_hoc_metric",
                    "metric": {"name_ru": phrase},
                }
                for index, phrase in enumerate(decision["scenarios"])
            ],
        ]
    return name, {key: value for key, value in args.items() if value not in (None, [], "")}


def without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def tool_calls_of(decisions: Sequence[Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    return [arguments_of(decision) for decision in decisions]


def scenario_of(
    name: str,
    question: str,
    calls: Sequence[tuple[str, Mapping[str, Any]]],
    answer: str,
) -> dict[str, Any]:
    """Сценарий подставной модели: круг k — k-й вызов, последний круг — текст ответа.

    `skill_read` называется как есть, `aifa_*` — с префиксом расширения."""
    rules: list[dict[str, Any]] = [
        {
            "id": "title-by-light-model",
            "when": {"model": "stub-light"},
            "reply": {"text": "Реплей"},
        },
    ]
    for round_no, (tool, args) in enumerate(calls, start=1):
        called = tool if tool == "skill_read" else TOOL.format(name=tool)
        rules.append(
            {
                "id": f"{round_no:02d}-{tool}",
                "when": {"model": "stub-main", "user_contains": question, "round": round_no},
                "reply": {"tool_calls": [{"name": called, "arguments": dict(args)}]},
            }
        )
    rules.append(
        {
            "id": "answer",
            "when": {"model": "stub-main", "user_contains": question, "round": len(calls) + 1},
            "reply": {"text": answer},
        }
    )
    return {"name": name, "rules": rules}


def skill_reads(*paths: str) -> list[tuple[str, dict[str, Any]]]:
    """Чтение навыка: сам `SKILL.md`, затем справочники, на которые он ссылается."""
    return [
        ("skill_read", {"skill": SKILL}),
        *(("skill_read", {"skill": SKILL, "path": path}) for path in paths),
    ]
