"""Цикл открывает стандартный путь и поднимает сохранённое после перезапуска (ADR-0023, ADR-0029, шаг 13).

Настоящие `parse_message`, `execute_routed_goal`, `CaseRuntime`, `LoopDesk` и хранилище состояния; подставные —
модель разбора цели, пара `locate`/`read.value` и документ (опоры сквозных тестов чата, `tests/integration`).
"""
# ruff: noqa: F811 — `gate_env` и `severstal_text` подключены как фикстуры
from __future__ import annotations

import pytest
from loop_support import Gateway
from test_standard_path import CUSTOM, MESSAGE, REVENUE, documents, with_revenue  # noqa: F401 — `documents`: autouse
from tests.integration.conftest import severstal_text  # noqa: F401
from tests.integration.ingress_support import (  # noqa: F401
    DOC,
    OWNER,
    gate_env,
    set_intent,
    set_intent_sequence,
)
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant import ouro_view
from finengine.assistant.goal_result import Verdict
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.standard_path import StandardPath
from finengine.assistant.tool_api import ToolApi
from finengine.config import Settings, settings
from finengine.store import state

CHAT = "p-lukoil"


@pytest.fixture(autouse=True)
def ouroboros_leads(monkeypatch: pytest.MonkeyPatch, coordination) -> None:
    """Ведёт цикл ouroboros; модель планировщика службы звать нельзя — иначе это снова путь `engine`."""
    monkeypatch.setattr(settings, "loop_driver", "ouroboros")

    async def forbidden(*args, **kwargs):
        raise AssertionError("модель-планировщик службы вызвана при loop_driver=ouroboros")

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", forbidden)


def test_the_service_leads_the_cycle_by_default() -> None:
    """Переключатель: по умолчанию `engine`, поведение как после шага 12; удаляется в шаге 19 (PRINCIPLES А.7)."""
    assert Settings.model_fields["loop_driver"].default == "engine"


async def test_when_the_standard_path_cannot_plan_the_cycle_is_handed_over(monkeypatch) -> None:
    """LR-40, §3.0: сбой стандартного пути — `НУЖЕН_ЦИКЛ` с состоянием; цикл жив у `LoopDesk`, служба его не гонит."""
    set_intent(monkeypatch, CUSTOM)
    with_revenue(monkeypatch)
    run = open_run(user_id=OWNER, owner_id=str(OWNER), chat_id=CHAT, case_id=CHAT, doc_ids=(DOC,), message=MESSAGE)
    desk = LoopDesk()
    events: list[dict] = []

    result = await StandardPath(desk)(run.record, events.append)
    text = ouro_view.goal(result, limit=12_000)

    assert result.verdict is Verdict.NEEDS_LOOP and first_line_of(text) == "НУЖЕН_ЦИКЛ"
    assert "## Вопрос пользователя" in text and "Детерминированный планировщик не собрал план" in text
    assert desk.live(run.run_id) is not None and desk.live(run.run_id).session.turns == 0
    assert "dynamic_move" not in [event["type"] for event in events], "ходов цикла служба не делала"
    refusals = [e["error"]["code"] for e in events if e["type"] == "typed_error"]
    assert "scenario_not_deterministic" not in refusals
    await run.end()


async def test_the_cycle_survives_a_restart_of_the_process(monkeypatch) -> None:
    """ADR-0023: состояние сохранено после открытия; новый процесс (пустой `LoopDesk`) поднимает его и ведёт дальше."""
    set_intent(monkeypatch, CUSTOM)
    with_revenue(monkeypatch)
    run = open_run(user_id=OWNER, owner_id=str(OWNER), chat_id=CHAT, case_id=CHAT, doc_ids=(DOC,), message=MESSAGE)
    await StandardPath(LoopDesk())(run.record, lambda event: None)
    assert state.get(state.LOOP_SESSION, owner=OWNER, key=run.run_id) is not None

    restarted = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), StandardPath(restarted), restarted))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    reading = await plugin.call("aifa_state", ctx)
    closed = await plugin.call("aifa_finish", ctx, {"action": "give_up"})

    assert first_line(reading) == "ГОТОВО" and "## Вопрос пользователя" in reading.text
    assert MESSAGE in reading.text
    assert first_line(closed) == "ПРИНЯТО" and restarted.live(run.run_id).session.closed
    await plugin.close()
    await run.end()


async def test_a_turn_of_another_owner_finds_no_saved_cycle(monkeypatch) -> None:
    """Граница владельца: сохранённый цикл читается только под ключом владельца хода."""
    set_intent(monkeypatch, CUSTOM)
    with_revenue(monkeypatch)
    run = open_run(user_id=OWNER, owner_id=str(OWNER), chat_id=CHAT, case_id=CHAT, doc_ids=(DOC,), message=MESSAGE)
    await StandardPath(LoopDesk())(run.record, lambda event: None)

    assert state.get(state.LOOP_SESSION, owner=OWNER + 1, key=run.run_id) is None
    await run.end()


def first_line_of(text: str) -> str:
    return text.split("\n", 1)[0]


async def _opened(monkeypatch, scenario_intent: dict):
    """Цикл открыт стандартным путём; дальше его ведёт клиент-ouroboros. Первый разбор — цель, второй — сценарий."""
    set_intent_sequence(monkeypatch, [CUSTOM, scenario_intent])
    with_revenue(monkeypatch)
    run = open_run(user_id=OWNER, owner_id=str(OWNER), chat_id=CHAT, case_id=CHAT, doc_ids=(DOC,), message=MESSAGE)
    desk = LoopDesk()
    await StandardPath(desk)(run.record, lambda event: None)
    plugin = Plugin(ToolApi(Gateway(), StandardPath(desk), desk))
    return run, plugin, ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)


MOVE = {"objective": "получить выручку", "reason": "нужна для ответа", "scenarios": ["Извлеки выручку за 2024 год"]}


async def test_a_scenario_of_a_move_goes_the_same_path_as_a_chat_message(monkeypatch) -> None:
    """LR-13, LR-15: сценарий идёт тем же разбором и конвейером; на экран из него — только `plan_graph`/`node_run`."""
    run, plugin, ctx = await _opened(monkeypatch, REVENUE)

    reply = await plugin.call("aifa_move", ctx, MOVE)
    await run.end()
    kinds = {event["type"] for event in await run.events()}

    assert first_line(reply) == "ГОТОВО" and "✓ extract_registered_metric" in reply.text
    assert "987654" in reply.text.replace(" ", "").replace(" ", ""), "значение — в новых артефактах хода"
    assert {"plan_graph", "node_run", "dynamic_move"} <= kinds
    assert not kinds & {"typed_result", "typed_error"}, "карточки и отказы сценария пользователю не видны"
    await plugin.close()


async def test_a_scenario_of_a_move_never_opens_a_nested_cycle(monkeypatch) -> None:
    """LR-14: сценарий без вложенного цикла — цель без корня в графе даёт отказ, а не новый цикл и не модель службы."""
    run, plugin, ctx = await _opened(monkeypatch, CUSTOM)

    reply = await plugin.call("aifa_move", ctx, MOVE)

    assert first_line(reply) == "ГОТОВО" and "scenario_not_deterministic" in reply.text
    await plugin.close()
    await run.end()
