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
from finengine.assistant.loop_opening import NoDocument
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord

pytestmark = pytest.mark.usefixtures("coordination")

CONTRACT_NAMES = Path(__file__).resolve().parents[3] / "contracts" / "tool_names.json"
DONE = {"label": "итог", "status": "done", "note": "атом сдан"}


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


class NoReport:
    """Открытие цикла, которому не по чему открыться: исход `aifa_open` — отказ «нет документа»."""

    async def __call__(self, record: TurnRecord) -> NoDocument:
        return NoDocument("Текст отчёта не готов.")


def types_of(events: list[dict]) -> list[str]:
    return [str(e["type"]) for e in events]


async def events_of(tool: str, args: dict | None = None, **ctx) -> tuple[list[dict], str]:
    plugin = Plugin(ToolApi(Gateway(), NoReport()))
    run = open_run()
    reply = await plugin.call(tool, ctx_of(run.run_id, **ctx), args)
    await run.end()
    await plugin.close()
    return await run.events(), reply.text


async def test_the_screen_gets_the_familiar_name_of_the_tool_not_the_name_ouroboros_knows() -> None:
    events, _ = await events_of("aifa_open")

    named = [(e["type"], e["name"]) for e in events if e["type"] in {"tool_call", "tool_output"}]
    assert named == [("tool_call", "read_source"), ("tool_output", "read_source")]


async def test_a_ready_result_is_a_successful_tool_output_and_a_refusal_is_not() -> None:
    ready, _ = await events_of("aifa_report", DONE, task_id="sub-1", root_task_id="root-task", task_depth=1)
    refused, text = await events_of("aifa_open")

    assert text.startswith("ОТКАЗ нет_документа")
    assert [e["ok"] for e in ready if e["type"] == "tool_output"] == [True]
    assert [e["ok"] for e in refused if e["type"] == "tool_output"] == [False]


async def test_every_call_of_a_turn_is_its_own_pair_so_the_screen_counts_them() -> None:
    plugin = Plugin(ToolApi(Gateway(), NoReport()))
    run = open_run()

    await plugin.call("aifa_open", ctx_of(run.run_id))
    await plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": "unknown", "wait_sec": 10})
    await run.end()
    await plugin.close()

    calls = [e["type"] for e in await run.events() if e["type"] in {"tool_call", "tool_output"}]
    assert calls == ["tool_call", "tool_output", "tool_call", "tool_output"]


async def test_a_call_that_is_not_of_this_turn_writes_nothing_into_any_journal() -> None:
    plugin = Plugin(ToolApi(Gateway(), NoReport()))
    run = open_run()
    bystander = open_run()

    reply = await plugin.call("aifa_open", ctx_of(run.run_id, owner_id="999"))
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
    api = ToolApi(Gateway(), NoReport())

    assert set(api.tool_names()) <= set(tool_names.SCREEN_NAMES)
