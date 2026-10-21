"""Сквозной тест ADR-0060: HTTP-клиент (плагин) → `aifa_open` → `aifa_move` с частью цели агента → конвейер на
фикстурном документе → журнал хода → `ouro_view`.

Настоящие: `tool_api`, привязка, задания, `LoopOpening`, `LoopDesk`, `LoopSession`, `GoalChannel`, `GraphExecutor`,
`CaseRuntime` (SQLite), прогон `run_orchestrator` и реплей журнала так, как его читает `/chat/runs/{id}/events`.
Подставные — пара `locate`/`read.value` и хранилище документов; модель разбора сообщения не вызывается вовсе.
"""
from __future__ import annotations

import pytest
from tests.integration.conftest import (
    _make_fake_locate_and_read_value_executors,
    severstal_text,  # noqa: F401 — фикстура для `gate_env`
)
from tests.integration.ingress_support import (  # noqa: F401 — `gate_env`: autouse-окружение
    DOC,
    OWNER,
    gate_env,
    patched_build_registry,
)
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    Plugin,
    coordination,
    ctx_of,
    first_line,
    open_run,
)

from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_opening import LoopOpening
from finengine.assistant.tool_api import ToolApi
from finengine.contracts.transport import LegacyCompatibilityEvent
from finengine.skills import run_orchestrator as ro
from finengine.skills.spec.dynamic_run import case_rows
from finengine.store.cases import get_case_store

pytestmark = pytest.mark.usefixtures("coordination")

CASE = "p-lukoil"
REVENUE_GOAL = {
    "objective": "выручка за 2024 год", "reason": "о ней спросил пользователь",
    "goals": [{"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["SALES_REVENUE"],
               "period": "p-2024-fy"}],
}


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


async def _no_classifier(*_args, **_kwargs) -> str:
    raise AssertionError("ADR-0060: сообщение не разбирается моделью-классификатором")


@pytest.fixture(autouse=True)
def pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _no_classifier)
    locate, read_value = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    patched_build_registry(monkeypatch, locate, read_value)


def _plugin() -> Plugin:
    desk = LoopDesk()
    return Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))


async def test_the_agent_opens_the_loop_and_its_goal_runs_the_pipeline_without_the_classifier() -> None:
    """ADR-0060 пп. 1–2: `aifa_open` открывает цикл без разбора; часть цели агента исполняет конвейер."""
    plugin = _plugin()
    run = open_run(
        owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,),
        message="какая выручка за 2024 год?",
    )
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CASE)

    opened = await plugin.call("aifa_open", ctx)
    moved = await plugin.call("aifa_move", ctx, REVENUE_GOAL)
    await run.end()
    await plugin.close()

    assert opened.status_code == 200 and first_line(opened) == "ГОТОВО"
    assert "## Реестр показателей" in opened.text and "сообщение служба не разбирала" in opened.text
    assert first_line(moved) == "ГОТОВО", moved.text
    assert "ExtractedMetric | SALES_REVENUE" in moved.text and "987654" in moved.text
    assert len(moved.text) <= 12_000


async def test_the_pipeline_events_are_in_the_journal_of_this_turn_as_the_screen_reads_it() -> None:
    plugin = _plugin()
    run = open_run(owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,))
    bystander = open_run(owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE)
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CASE)

    await plugin.call("aifa_open", ctx)
    await plugin.call("aifa_move", ctx, REVENUE_GOAL)
    await run.end()
    await bystander.end()
    await plugin.close()

    replay = [e async for e in ro.subscribe_versioned(run.run_id, kind="chat")]
    names = [
        e.legacy_event["type"] if isinstance(e, LegacyCompatibilityEvent) else e.event_type
        for e in replay
    ]
    assert "plan_graph" in names and "node_run" in names, names
    assert names.index("plan_graph") < names.index("node_run")
    assert [e.sequence for e in replay] == list(range(1, len(replay) + 1)), "порядок журнала сплошной"
    other = [e async for e in ro.subscribe_versioned(bystander.run_id, kind="chat")]
    assert "plan_graph" not in [
        e.legacy_event["type"] if isinstance(e, LegacyCompatibilityEvent) else e.event_type
        for e in other
    ], "события чужого хода в журнал этого не попадают"


async def test_a_foreign_owner_cannot_open_the_loop_of_this_turn() -> None:
    plugin = _plugin()
    run = open_run(owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,))

    reply = await plugin.call("aifa_open", ctx_of(run.run_id, owner_id="999", chat_id=CASE))
    await run.end()
    await plugin.close()

    assert first_line(reply) == "ОТКАЗ чужой_ход"
    assert not case_rows(get_case_store(), OWNER, CASE), "Case не тронут"
