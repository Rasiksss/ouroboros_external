"""`tool_call` и `tool_output` в журнале хода на каждый вызов инструмента (CONTRACTS.md §2.5, шаг 10).

Имена для экрана — прежние названия инструментов AIFA (`aifa/contracts/tool_names.json`), чтобы
экран показывал знакомое. Журнал читается так, как его читает ручка `/chat/runs/{id}/events`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    Plugin,
    coordination,
    ctx_of,
    open_run,
)

from finengine.assistant import tool_names
from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord

pytestmark = pytest.mark.usefixtures("coordination")

CONTRACT_NAMES = Path(__file__).resolve().parents[3] / "contracts" / "tool_names.json"


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


class Pipeline:
    """Цель, которая пишет в журнал хода событие конвейера и отвечает заданным исходом."""

    def __init__(self, result: GoalResult) -> None:
        self.result = result

    async def __call__(self, record: TurnRecord, emit) -> GoalResult:
        emit({"type": "plan_graph", "planner_version": "test"})
        return self.result


def types_of(events: list[dict]) -> list[str]:
    return [str(e["type"]) for e in events]


async def goal_events(result: GoalResult) -> tuple[list[dict], str]:
    plugin = Plugin(ToolApi(Gateway(), Pipeline(result)))
    run = open_run()
    reply = await plugin.call("aifa_goal", ctx_of(run.run_id))
    await run.end()
    await plugin.close()
    return await run.events(), reply.text


async def test_a_tool_call_wraps_the_events_of_the_pipeline_in_the_journal_of_the_turn() -> None:
    """Порядок экрана: `tool_call` → события конвейера → `tool_output` (плюс служебное)."""
    events, _ = await goal_events(GoalResult(Verdict.ANSWERED, "Готово."))

    types = types_of(events)
    assert types.index("tool_call") < types.index("plan_graph") < types.index("tool_output")


async def test_the_screen_gets_the_familiar_name_of_the_tool_not_the_name_ouroboros_knows() -> None:
    events, _ = await goal_events(GoalResult(Verdict.ANSWERED, "Готово."))

    named = [(e["type"], e["name"]) for e in events if e["type"] in {"tool_call", "tool_output"}]
    assert named == [("tool_call", "financial_pipeline"), ("tool_output", "financial_pipeline")]


async def test_a_ready_result_is_a_successful_tool_output_and_a_refusal_is_not() -> None:
    ready, _ = await goal_events(GoalResult(Verdict.ANSWERED, "Готово."))
    refused, text = await goal_events(GoalResult(Verdict.NO_DOCUMENT, "Текст отчёта не готов."))

    assert text.startswith("ОТКАЗ нет_документа")
    assert [e["ok"] for e in ready if e["type"] == "tool_output"] == [True]
    assert [e["ok"] for e in refused if e["type"] == "tool_output"] == [False]


async def test_a_question_to_the_user_is_not_a_failed_tool() -> None:
    events, text = await goal_events(GoalResult(Verdict.CLARIFY, "Какой период?"))

    assert text.startswith("УТОЧНИ")
    assert [e["ok"] for e in events if e["type"] == "tool_output"] == [True]


async def test_every_call_of_a_turn_is_its_own_pair_so_the_screen_counts_them() -> None:
    plugin = Plugin(ToolApi(Gateway(), Pipeline(GoalResult(Verdict.ANSWERED, "Готово."))))
    run = open_run()

    await plugin.call("aifa_goal", ctx_of(run.run_id))
    await plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": "unknown", "wait_sec": 10})
    await run.end()
    await plugin.close()

    calls = [e["type"] for e in await run.events() if e["type"] in {"tool_call", "tool_output"}]
    assert calls == ["tool_call", "tool_output", "tool_call", "tool_output"]


async def test_a_call_that_is_not_of_this_turn_writes_nothing_into_any_journal() -> None:
    plugin = Plugin(ToolApi(Gateway(), Pipeline(GoalResult(Verdict.ANSWERED, "Готово."))))
    run = open_run()
    bystander = open_run()

    reply = await plugin.call("aifa_goal", ctx_of(run.run_id, owner_id="999"))
    await run.end()
    await bystander.end()
    await plugin.close()

    assert reply.text.startswith("ОТКАЗ чужой_ход")
    for journal in (await run.events(), await bystander.events()):
        assert "tool_call" not in types_of(journal)


def test_the_screen_names_in_the_service_are_the_contract_file() -> None:
    """`aifa/contracts/tool_names.json` — контракт, копия в службе (в образ каталог `aifa/` не едет) обязана совпасть."""
    assert json.loads(CONTRACT_NAMES.read_text(encoding="utf-8")) == tool_names.SCREEN_NAMES


def test_every_tool_the_service_serves_has_a_screen_name() -> None:
    api = ToolApi(Gateway(), Pipeline(GoalResult(Verdict.ANSWERED)))

    assert set(api.tool_names()) <= set(tool_names.SCREEN_NAMES)
