"""`aifa_goal` и `aifa_wait`: фоновые задания, длинный опрос, идемпотентность, отмена хода (C3.3, C3.4).

Цель хода здесь — подставной `GoalSource` с управляемым временем: что стандартный путь отдаёт сам,
проверяют `test_standard_path.py` и сквозной тест. Прогон, привязка, журнал хода — настоящие.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from tool_harness import (  # noqa: F401 — `coordination`: фикстура окружения
    Plugin,
    coordination,
    ctx_of,
    first_line,
    open_run,
)

from finengine.assistant import goal_tools
from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_record import TurnRecord
from finengine.config import settings
from finengine.skills import run_orchestrator as ro

pytestmark = pytest.mark.usefixtures("coordination")

WAITING = re.compile(r"^ЖДИ job=([0-9a-f]+)$")
ANSWER = GoalResult(Verdict.ANSWERED, "Расчёт выполнен.")


@dataclass
class TimedGoal:
    """Цель, которая идёт `delay` секунд и пишет в журнал хода одно событие конвейера."""

    delay: float
    fail: bool = False
    calls: int = 0
    cancelled: int = 0
    seen: list[str] = field(default_factory=list)

    async def __call__(self, record: TurnRecord, emit) -> GoalResult:
        self.calls += 1
        self.seen.append(record.message)
        emit({"type": "plan_graph", "planner_version": "test"})
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.fail:
            raise RuntimeError("конвейер упал")
        return ANSWER


@pytest.fixture(autouse=True)
def quick_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отрезок вызова и сверка с ходом — доли секунды; нижний предел `wait_sec` снят ради скорости теста."""
    monkeypatch.setattr(settings, "ouroboros_tool_segment_sec", 0.2)
    monkeypatch.setattr(settings, "ouroboros_run_watch_sec", 0.05)
    monkeypatch.setattr(goal_tools, "WAIT_MIN_SEC", 0)


class Gateway:
    def root_known(self, run_id: str, task_id: str) -> bool:
        return True


@pytest.fixture
async def plugin_with() -> AsyncIterator[Callable[[TimedGoal], Plugin]]:
    clients: list[Plugin] = []

    def make(goal: TimedGoal) -> Plugin:
        client = Plugin(ToolApi(Gateway(), goal))
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


async def test_a_short_goal_is_answered_by_the_same_call(plugin_with) -> None:
    goal = TimedGoal(delay=0.0)
    plugin = plugin_with(goal)
    run = open_run()

    reply = await plugin.call("aifa_goal", ctx_of(run.run_id))

    assert (reply.status_code, first_line(reply)) == (200, "ГОТОВО")
    assert goal.seen == ["какая выручка за 2024 год?"], "цель — исходное сообщение прогона"
    await run.end()


async def test_a_long_goal_waits_in_the_background_and_aifa_wait_returns_its_result(
    plugin_with,
) -> None:
    """C3.4: `ЖДИ job=<id>` → `aifa_wait` → итог."""
    plugin = plugin_with(TimedGoal(delay=0.7))
    run = open_run()

    waiting = await plugin.call("aifa_goal", ctx_of(run.run_id))
    job_id = WAITING.match(first_line(waiting)).group(1)
    still = await plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": job_id, "wait_sec": 0.1})
    done = await plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": job_id, "wait_sec": 5})

    assert first_line(still) == f"ЖДИ job={job_id}"
    assert (done.status_code, first_line(done)) == (200, "ГОТОВО")
    assert "Расчёт выполнен." in done.text
    await run.end()


async def test_the_goal_is_answered_once_per_turn_and_the_repeat_gets_the_same_result(
    plugin_with,
) -> None:
    """C3.3: повторный вызов `aifa_goal` в том же ходе — тот же итог, цель не разбирается заново."""
    goal = TimedGoal(delay=0.0)
    plugin = plugin_with(goal)
    run = open_run()

    first = await plugin.call("aifa_goal", ctx_of(run.run_id))
    second = await plugin.call("aifa_goal", ctx_of(run.run_id))

    assert first.text == second.text and first_line(first) == "ГОТОВО"
    assert goal.calls == 1
    await run.end()


async def test_a_repeat_while_the_goal_runs_points_to_the_same_job(plugin_with) -> None:
    goal = TimedGoal(delay=0.7)
    plugin = plugin_with(goal)
    run = open_run()

    first = await plugin.call("aifa_goal", ctx_of(run.run_id))
    second = await plugin.call("aifa_goal", ctx_of(run.run_id))

    assert first.text == second.text and WAITING.match(first_line(first))
    assert goal.calls == 1
    await run.end()


async def test_two_turns_get_their_own_goals(plugin_with) -> None:
    goal = TimedGoal(delay=0.0)
    plugin = plugin_with(goal)
    one, other = open_run(message="первый вопрос"), open_run(message="второй вопрос")

    await plugin.call("aifa_goal", ctx_of(one.run_id))
    await plugin.call("aifa_goal", ctx_of(other.run_id))

    assert goal.seen == ["первый вопрос", "второй вопрос"]
    await one.end()
    await other.end()


async def test_a_failed_goal_is_a_service_fault_and_the_next_call_tries_again(plugin_with) -> None:
    """Системная ошибка — 5xx; сорвавшийся итог не запоминается, повтор идёт заново."""
    goal = TimedGoal(delay=0.0, fail=True)
    plugin = plugin_with(goal)
    run = open_run()

    crashed = await plugin.call("aifa_goal", ctx_of(run.run_id))
    goal.fail = False
    retried = await plugin.call("aifa_goal", ctx_of(run.run_id))

    assert crashed.status_code == 500 and first_line(crashed).startswith("СБОЙ_СЛУЖБЫ")
    assert (retried.status_code, first_line(retried)) == (200, "ГОТОВО")
    assert goal.calls == 2
    await run.end()


async def test_the_events_of_the_goal_land_in_the_journal_of_this_turn(plugin_with) -> None:
    plugin = plugin_with(TimedGoal(delay=0.0))
    run = open_run()
    other = open_run()

    await plugin.call("aifa_goal", ctx_of(run.run_id))
    await run.end()
    await other.end()

    assert {"type": "plan_graph", "planner_version": "test"} in await run.events()
    assert {"type": "plan_graph", "planner_version": "test"} not in await other.events()


async def test_a_job_of_another_turn_is_not_found(plugin_with) -> None:
    plugin = plugin_with(TimedGoal(delay=0.7))
    mine, other = open_run(), open_run()
    waiting = await plugin.call("aifa_goal", ctx_of(mine.run_id))
    job_id = WAITING.match(first_line(waiting)).group(1)

    stolen = await plugin.call("aifa_wait", ctx_of(other.run_id), {"job_id": job_id, "wait_sec": 1})

    assert first_line(stolen) == "ОТКАЗ задание_не_найдено"
    await mine.end()
    await other.end()


async def test_cancelling_the_turn_interrupts_the_waiting_and_stops_the_job(plugin_with) -> None:
    """C3.4: «Стоп» хода (`cancel_run`) прерывает `aifa_wait`, задание снимается."""
    goal = TimedGoal(delay=30.0)
    plugin = plugin_with(goal)
    run = open_run()
    waiting = await plugin.call("aifa_goal", ctx_of(run.run_id))
    job_id = WAITING.match(first_line(waiting)).group(1)

    waiter = asyncio.create_task(
        plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": job_id, "wait_sec": 20})
    )
    await asyncio.sleep(0.1)
    assert ro.cancel_run(run.run_id) is True
    reply = await asyncio.wait_for(waiter, timeout=3)

    assert first_line(reply) == "ОТКАЗ чужой_ход"
    await asyncio.sleep(0.2)
    assert goal.cancelled == 1, "фоновая задача цели остановлена вместе с ходом"


async def test_a_job_nobody_waits_for_is_stopped_with_the_turn(plugin_with) -> None:
    goal = TimedGoal(delay=30.0)
    plugin = plugin_with(goal)
    run = open_run()
    await plugin.call("aifa_goal", ctx_of(run.run_id))

    ro.cancel_run(run.run_id)
    await asyncio.sleep(0.3)

    assert goal.cancelled == 1
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("ouro-job-") and not t.done()]


@pytest.mark.parametrize(
    "args",
    [
        {"wait_sec": 10},
        {"job_id": "", "wait_sec": 10},
        {"job_id": "abc"},
        {"job_id": "abc", "wait_sec": "много"},
        {"job_id": "abc", "wait_sec": 241},
        {"task_id": "sub-1", "wait_sec": 10},
    ],
)
async def test_aifa_wait_refuses_arguments_outside_the_contract(plugin_with, args: dict) -> None:
    plugin = plugin_with(TimedGoal(delay=0.0))
    run = open_run()

    reply = await plugin.call("aifa_wait", ctx_of(run.run_id), args)

    assert (reply.status_code, first_line(reply)) == (200, "ОТКАЗ неверные_аргументы")
    await run.end()


async def test_aifa_wait_refuses_a_wait_below_the_contract_minimum(
    plugin_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(goal_tools, "WAIT_MIN_SEC", 10)
    plugin = plugin_with(TimedGoal(delay=0.0))
    run = open_run()

    reply = await plugin.call("aifa_wait", ctx_of(run.run_id), {"job_id": "abc", "wait_sec": 9})

    assert first_line(reply) == "ОТКАЗ неверные_аргументы"
    await run.end()
