"""Сквозной тест шага 16: корень → `schedule_task` → подзадача ouroboros → `tool_api` →
`atom_intake` → комната →
`Case` → `LoopSession` → журнал хода; кадр итога → `turn_gateway` → `subtask_waits` → `aifa_wait`
(CONTRACTS.md §4).

Все звенья настоящие, кроме модели (подставной сервер), границ конвейера (`pipeline_fixture.py`) и
комнаты станции
(`room_stub.py`: та же точка входа, локальный интерпретатор). Навык подзадачи — фикстура C4.7 из
`aifa/tests/contract/skills/`; служба навыка не знает: подзадачу ведёт её модель.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from room_stub import use_local_room
from test_chat_turn_end_to_end import kind_of, screen_events, start_turn
from test_loop_end_to_end import CUSTOM_INTENT

from stand import SCENARIOS, ContractStand
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

NAME = "eph.revenue_double"
QUESTION = "какая выручка за 2024 год? Нужен атом."
SILENT = "Нужна тишина."


@pytest.fixture
def engine(stand: ContractStand, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Служба с циклом, который ведёт ouroboros; разбор цели — вид без корня, затем выручка."""
    from finengine.config import settings

    replies = iter([CUSTOM_INTENT])

    async def parsed(system: str, prompt: str, *, model_version: Any = None) -> str:
        return json.dumps(next(replies, fixture.INTENT), ensure_ascii=False)

    async def planner_of_the_service(system: str, prompt: str) -> str:
        raise AssertionError("модель-планировщик службы вызвана при loop_driver=ouroboros")

    with running_engine(stand, tmp_path, monkeypatch) as base:
        monkeypatch.setattr(settings, "loop_driver", "ouroboros")
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", parsed)
        monkeypatch.setattr(
            "finengine.skills.spec.dynamic_run._default_complete", planner_of_the_service
        )
        use_local_room(monkeypatch)
        yield base


async def until_quiet(
    stub: StubModelAdmin, still_for_sec: float = 3.0, at_most_sec: float = 60.0
) -> None:
    """Подождать, пока подставная модель перестанет получать запросы: снятая подзадача доживает
    круг-другой,
    и эти запросы не должны попасть в журнал следующего теста."""
    loop = asyncio.get_running_loop()
    deadline, last, changed_at = loop.time() + at_most_sec, -1, loop.time()
    while loop.time() < deadline and loop.time() - changed_at < still_for_sec:
        await asyncio.sleep(0.5)
        if (count := len(stub.journal())) != last:
            last, changed_at = count, loop.time()


def results_of(stub: StubModelAdmin, user_marker: str) -> dict[int, str]:
    """Результаты инструментов, которые увидела модель задачи, — по номеру круга."""
    return {
        e["round"]: e["last_tool_result"]
        for e in stub.journal()
        if e["model"] == "stub-main" and user_marker in str(e["user"])
    }


async def test_a_subtask_of_another_skill_registers_an_atom_and_the_root_uses_it(
    engine: str, stub: StubModelAdmin
) -> None:
    """C4.1, C4.3, C4.4, C4.6, C4.7: подзадача по фикстурному навыку проходит весь путь без правок
    службы."""
    stub.use_scenario(SCENARIOS / "delegation.json")
    run_id = await start_turn(engine, QUESTION)

    events = await screen_events(engine, run_id)

    sub = results_of(stub, "Навык: contract-fixture-role")
    root = results_of(stub, "Нужен атом")
    # C4.1: вызовы подзадачи принимаются (область и корень те же), а `aifa_finish` из неё — не та
    # роль.
    assert sub[2].startswith("ГОТОВО"), "aifa_state подзадачи принят"
    assert sub[3].startswith("ОТКАЗ не_та_роль")
    # C4.3: первая регистрация — ОТКАЗ с хвостом ошибки, вторая — ГОТОВО.
    assert sub[5].startswith("ОТКАЗ неверные_аргументы") and "ZeroDivisionError" in sub[5]
    assert sub[6].startswith("ГОТОВО\nАтом eph.revenue_double@") and "revenue_x2" in sub[6]
    # C4.6: корень получил итог подзадачи (note её `aifa_report done`) и сделал шаг с принятым
    # атомом.
    assert root[5].startswith("ГОТОВО\n## Итог подзадачи") and "принят службой" in root[5]
    assert root[6].startswith("ГОТОВО"), root[6]
    assert root[7].startswith("ПРИНЯТО")
    # C4.4: попытки одного имени нумеруются одним счётчиком в пределах хода цикла: отчёт, отказ,
    # успех.
    attempts = [
        (e["legacy_event"]["move"], e["legacy_event"]["attempt"], e["legacy_event"]["status"])
        for e in events
        if kind_of(e) == "author_attempt"
    ]
    assert attempts == [(1, 1, "error"), (1, 2, "error"), (1, 3, "ok")], attempts
    assert events[-1]["status"] == "succeeded"
    assert stub.unmatched() == []


async def test_a_silent_subtask_is_refused_by_the_term_and_the_root_cancels_it(
    engine: str, stub: StubModelAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4.9: молчащая подзадача — `подзадача_не_ответила` по сроку, с подсказкой `cancel_task`;
    корень её снимает."""
    from finengine.config import settings

    monkeypatch.setattr(settings, "ouroboros_subtask_wait_sec", 3.0)
    stub.use_scenario(SCENARIOS / "silent_subtask.json")
    run_id = await start_turn(engine, SILENT)

    events = await screen_events(engine, run_id)
    await until_quiet(stub)

    root = results_of(stub, SILENT)
    assert root[3].startswith("ОТКАЗ подзадача_не_ответила")
    assert "cancel_task" in root[3] and "get_task_result" in root[3]
    assert root[4].startswith("Cancel requested")
    assert events[-1]["status"] == "succeeded"
