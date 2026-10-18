"""Сквозной тест шага 09: HTTP-клиент (плагин) → `tool_api` → конвейер на фикстурном документе → журнал хода → `ouro_view`.

Настоящие: `tool_api`, привязка, задания, `StandardPath`, `parse_message`, `GraphExecutor`, `CaseRuntime`
(SQLite), прогон `run_orchestrator` и реплей журнала так, как его читает `/chat/runs/{id}/events`.
Подставные — модель разбора и пара `locate`/`read.value`, хранилище документов.
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
    FakeMeta,
    FakeStore,
    gate_env,
    patched_build_registry,
    set_intent,
)
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    Plugin,
    coordination,
    ctx_of,
    first_line,
    open_run,
)

from finengine.assistant import standard_path
from finengine.assistant.standard_path import StandardPath
from finengine.assistant.tool_api import ToolApi
from finengine.contracts.transport import LegacyCompatibilityEvent
from finengine.skills import run_orchestrator as ro
from finengine.skills.spec.dynamic_run import artifacts_table, case_rows
from finengine.store.cases import get_case_store

pytestmark = pytest.mark.usefixtures("coordination")

REVENUE = {
    "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "entity_ref": None, "period_ref": "p-2024-fy", "role": None, "unresolved": [], "ambiguities": [],
}
CASE = "p-lukoil"


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(standard_path, "get_store", lambda: FakeStore({DOC: FakeMeta(OWNER)}))
    set_intent(monkeypatch, REVENUE)
    locate, read_value = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    patched_build_registry(monkeypatch, locate, read_value)


async def test_aifa_goal_over_http_runs_the_pipeline_and_answers_with_the_results_table() -> None:
    plugin = Plugin(ToolApi(Gateway(), StandardPath()))
    run = open_run(
        owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,),
        message="какая выручка за 2024 год?",
    )

    reply = await plugin.call(
        "aifa_goal", ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CASE)
    )
    await run.end()
    await plugin.close()

    assert reply.status_code == 200 and first_line(reply) == "ГОТОВО"
    table = artifacts_table(list(case_rows(get_case_store(), OWNER, CASE)))
    assert "## Полученные артефакты\nid | тип | концепт | период | значение\n" + table in reply.text
    assert "ExtractedMetric | SALES_REVENUE" in reply.text and "987654" in reply.text
    assert len(reply.text) <= 12_000


async def test_the_pipeline_events_are_in_the_journal_of_this_turn_as_the_screen_reads_it() -> None:
    plugin = Plugin(ToolApi(Gateway(), StandardPath()))
    run = open_run(
        owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,),
    )
    bystander = open_run(owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE)

    await plugin.call("aifa_goal", ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CASE))
    await run.end()
    await bystander.end()
    await plugin.close()

    replay = [e async for e in ro.subscribe_versioned(run.run_id, kind="chat")]
    names = [
        e.legacy_event["type"] if isinstance(e, LegacyCompatibilityEvent) else e.event_type
        for e in replay
    ]
    assert names.count("plan_graph") == 1 and "node_run" in names, names
    assert names.index("plan_graph") < names.index("node_run")
    assert [e.sequence for e in replay] == list(range(1, len(replay) + 1)), "порядок журнала сплошной"
    other = [e async for e in ro.subscribe_versioned(bystander.run_id, kind="chat")]
    assert "plan_graph" not in [
        e.legacy_event["type"] if isinstance(e, LegacyCompatibilityEvent) else e.event_type
        for e in other
    ], "события чужого хода в журнал этого не попадают"


async def test_a_foreign_owner_cannot_run_the_goal_of_this_turn() -> None:
    plugin = Plugin(ToolApi(Gateway(), StandardPath()))
    run = open_run(owner_id=str(OWNER), user_id=OWNER, chat_id=CASE, case_id=CASE, doc_ids=(DOC,))

    reply = await plugin.call("aifa_goal", ctx_of(run.run_id, owner_id="999", chat_id=CASE))
    await run.end()
    await plugin.close()

    assert first_line(reply) == "ОТКАЗ чужой_ход"
    assert not case_rows(get_case_store(), OWNER, CASE), "Case не тронут"
