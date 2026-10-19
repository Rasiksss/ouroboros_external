"""Сквозной тест шага 13: ouroboros (подставная модель) → `aifa_goal` `НУЖЕН_ЦИКЛ` → `aifa_doc` →
`aifa_move` → конвейер → журнал хода → `aifa_finish` → финал.

Все звенья настоящие, кроме модели (подставной сервер) и границ конвейера (`pipeline_fixture.py`):
ouroboros — контейнер профиля `contract` с навыками из этого дерева; служба — настоящее приложение,
цикл ведёт ouroboros (`loop_driver=ouroboros`). Модель планировщика службы не вызывается: её подмена
падает при вызове. Закрывает связность шага 13 (PRINCIPLES.md §Б.3): `tool_api` ↔ `LoopSession`,
конвейер ↔ журнал хода, служба ↔ ouroboros.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from test_chat_turn_end_to_end import kind_of, screen_events, start_turn

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

CUSTOM_INTENT = {
    "route": "plan",
    "intent_kind": "custom_analysis",
    "targets": [],
    "entity_ref": None,
    "period_ref": None,
    "role": None,
    "unresolved": ["сложный вопрос"],
    "ambiguities": [],
}
TOOL = "ext_13_r_aifa-engine_{name}"


@pytest.fixture
def engine(stand: ContractStand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Служба с циклом, который ведёт ouroboros: первый разбор цели — вид без корня в графе,
    остальные (сценарий хода) — выручка."""
    from finengine.config import settings

    replies = iter([CUSTOM_INTENT])

    async def parsed(system: str, prompt: str, *, model_version: Any = None) -> str:
        return json.dumps(next(replies, fixture.INTENT), ensure_ascii=False)

    async def planner_of_the_service(system: str, prompt: str) -> str:
        raise AssertionError("модель-планировщик службы вызвана при loop_driver=ouroboros")

    with running_engine(stand, tmp_path, monkeypatch) as base:
        monkeypatch.setattr(settings, "loop_driver", "ouroboros")
        monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", parsed)
        monkeypatch.setattr(
            "finengine.skills.spec.dynamic_run._default_complete", planner_of_the_service
        )
        yield base


async def run_the_cycle(engine: str, stub: StubModelAdmin) -> tuple[str, list[dict[str, Any]]]:
    stub.use_scenario(SCENARIOS / "loop.json")
    run_id = await start_turn(engine, "какая выручка за 2024 год?")
    return run_id, await screen_events(engine, run_id)


async def test_ouroboros_leads_the_cycle_to_the_final_answer(
    engine: str, stub: StubModelAdmin
) -> None:
    """Экран видит: цель, цикл, документ, ход (граф, ход цикла), конец цикла, ответ, конец."""
    _, events = await run_the_cycle(engine, stub)

    kinds = [kind_of(e) for e in events]
    assert kinds[0] == "session_id"
    assert kinds[-3:] == ["text_delta", "text_segment_end", "completed"]
    named = [
        (e["legacy_event"]["type"], e["legacy_event"]["name"])
        for e in events
        if kind_of(e) in {"tool_call", "tool_output"}
    ]
    assert named == [
        ("tool_call", "financial_pipeline"),
        ("tool_output", "financial_pipeline"),
        ("tool_call", "read_source"),
        ("tool_output", "read_source"),
        ("tool_call", "financial_pipeline"),
        ("tool_output", "financial_pipeline"),
        ("tool_call", "financial_pipeline"),
        ("tool_output", "financial_pipeline"),
    ], named
    moves = [e["legacy_event"] for e in events if kind_of(e) == "dynamic_move"]
    assert [(m["status"], m["action"]) for m in moves] == [
        ("running", "execute"),
        ("done", "execute"),
        ("done", "give_up"),
    ]
    assert kinds.count("plan_graph") == 1, "граф сценария хода показан один раз"
    assert events[-1]["status"] == "succeeded"


async def test_the_model_got_the_state_the_document_and_the_new_artifacts_from_the_service(
    engine: str, stub: StubModelAdmin
) -> None:
    """Модель получила по сети: состояние цикла с вопросом, документы хода, новые артефакты хода."""
    await run_the_cycle(engine, stub)

    asked = [e for e in stub.journal() if e["model"] == "stub-main"]
    results = {e["round"]: e["last_tool_result"] for e in asked}
    assert results[3].startswith("НУЖЕН_ЦИКЛ\n")
    assert "## Вопрос пользователя" in results[3]
    assert f"{fixture.DOC} | Северсталь | страниц:" in results[4]
    assert results[5].startswith("ГОТОВО\n## Итог хода 1") and "SALES_REVENUE" in results[5]
    assert "987654" in results[5].replace(" ", "").replace(" ", "")
    assert results[6].startswith("ПРИНЯТО\nЦикл закрыт.")
    assert stub.unmatched() == []
    for name in ("aifa_state", "aifa_doc", "aifa_move", "aifa_finish"):
        assert TOOL.format(name=name) in asked[0]["tools"], f"{name} не попал в первый запрос (F4)"


async def test_the_cycle_is_saved_after_every_move_and_closed_by_the_finish(
    engine: str, stub: StubModelAdmin
) -> None:
    """ADR-0023: состояние цикла лежит в хранилище службы; после `aifa_finish` оно закрыто."""
    from finengine.store import state

    run_id, _ = await run_the_cycle(engine, stub)

    saved = state.get(state.LOOP_SESSION, owner=fixture.OWNER, key=run_id)
    assert saved is not None and saved["state"]["closed"] is True
    assert saved["state"]["turns"] == 1 and len(saved["state"]["journal"]) == 1
    assert saved["origin"]["document_id"] == fixture.DOC
