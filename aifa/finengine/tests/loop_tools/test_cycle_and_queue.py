"""Цикл и очередь хода (CONTRACTS.md §3.0, C3.0, C3.8, C3.9, ADR-0043 пп. 4–5).

Изменяющие вызовы хода идут по одному, чтение — мимо очереди; цикл открывает только `НУЖЕН_ЦИКЛ`.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field

from loop_support import Gateway, open_cycle
from test_dynamic_run import ExtractScenario, _finalize, _move
from test_ephemeral_in_graph import CASE, OWNER
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant.goal_result import GoalResult, Verdict
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.skills.spec.dynamic_run import ScenarioResult

WAITING = re.compile(r"^ЖДИ job=([0-9a-f]+)$")


@dataclass
class Slow:
    """Сценарий-подставка, который занимает рабочий поток на `delay` секунд и помнит свои отрезки."""

    delay: float
    spans: list[tuple[float, float]] = field(default_factory=list)

    def __call__(self, text: str) -> ScenarioResult:
        start = time.monotonic()
        time.sleep(self.delay)
        self.spans.append((start, time.monotonic()))
        return ScenarioResult(True, "✓ готово")


@dataclass
class Held:
    """Сценарий-подставка, который держит ход, пока тест не отпустит его."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def __call__(self, text: str) -> ScenarioResult:
        self.started.set()
        self.release.wait(10)
        return ScenarioResult(True, "✓ готово")


async def test_changing_calls_of_one_turn_run_one_at_a_time(base) -> None:
    """C3.8: два одновременных изменяющих вызова хода не пересекаются, состояние цикла не портится."""
    slow = Slow(delay=0.3)
    cycle = await open_cycle(base, scenario=slow)

    replies = await asyncio.gather(
        cycle.move(_move(scenarios=("первый",))), cycle.move(_move(scenarios=("второй",)))
    )

    assert [first_line_of(reply) for reply in replies] == ["ГОТОВО", "ГОТОВО"]
    first, second = sorted(slow.spans)
    assert first[1] <= second[0], "второй ход начался, пока шёл первый"
    assert cycle.session.turns == 2 and len(cycle.session.journal) == 2
    await cycle.close()


async def test_reading_does_not_wait_for_a_running_move(base) -> None:
    """C3.8: `aifa_state` во время `aifa_move` отвечает сразу, не встаёт в очередь."""
    held = Held()
    cycle = await open_cycle(base, scenario=held)
    running = asyncio.create_task(cycle.move(_move(scenarios=("долгий",))))
    await asyncio.to_thread(held.started.wait, 5)

    state = await asyncio.wait_for(cycle.call("aifa_state"), timeout=2)
    held.release.set()

    assert first_line_of(state) == "ГОТОВО" and "## Вопрос пользователя" in state
    assert first_line_of(await running) == "ГОТОВО"
    await cycle.close()


async def test_a_move_that_outlasts_the_call_becomes_a_job_and_aifa_wait_returns_its_result(
    base, monkeypatch,
) -> None:
    """C3.8, C3.4: не уложился в срок вызова — `ЖДИ job=`; `aifa_wait` отдаёт итог хода."""
    monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.1)
    cycle = await open_cycle(base, scenario=Slow(delay=0.5))

    waiting = await cycle.move(_move(scenarios=("долгий",)))
    job_id = WAITING.match(first_line_of(waiting)).group(1)
    done = await cycle.call("aifa_wait", {"job_id": job_id, "wait_sec": 5})

    assert first_line_of(done) == "ГОТОВО" and "## Итог хода 1" in done
    await cycle.close()


async def test_a_call_that_waits_in_the_queue_counts_its_wait_into_its_own_term(base, monkeypatch) -> None:
    """C3.8: ожидание в очереди входит в срок вызова — вторая просьба за долгой первой тоже отвечает `ЖДИ`."""
    monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.15)
    cycle = await open_cycle(base, scenario=Slow(delay=0.5))

    first, second = await asyncio.gather(
        cycle.move(_move(scenarios=("а",))), cycle.move(_move(scenarios=("б",)))
    )

    assert WAITING.match(first_line_of(first)) and WAITING.match(first_line_of(second))
    await asyncio.sleep(1.2)
    assert cycle.session.turns == 2, "обе просьбы исполнены в свой черёд"
    await cycle.close()


async def test_after_the_finish_every_changing_call_is_refused_as_closed(base) -> None:
    """C3.8: после `aifa_finish` — `ОТКАЗ цикл_закрыт`."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA",)))
    concept = next(
        payload for row in base.store.list_artifacts(OWNER, CASE) for payload in row["payloads"]
        if getattr(payload, "concept", None) == "EBITDA"
    )

    assert first_line_of(await cycle.finish(_finalize(concept.scalar_fact.evidence_id))) == "ПРИНЯТО"
    assert first_line_of(await cycle.move(_move(scenarios=("ещё",)))) == "ОТКАЗ цикл_закрыт"
    assert first_line_of(await cycle.finish(_finalize("x"))) == "ОТКАЗ цикл_закрыт"
    await cycle.close()


async def test_a_subtask_may_move_the_cycle_but_not_close_it(base) -> None:
    """LR-28, C3.0: `aifa_finish` из подзадачи — `ОТКАЗ не_та_роль`; ход цикла подзадача делать может (ADR-0043 п. 2)."""
    cycle = await open_cycle(base, scenario=Slow(delay=0.0))

    assert first_line_of(await cycle.call("aifa_finish", {"action": "give_up"}, depth=1)) == "ОТКАЗ не_та_роль"
    assert first_line_of(await cycle.call("aifa_move", {**_args("подзадача")}, depth=1)) == "ГОТОВО"
    assert not cycle.session.closed
    await cycle.close()


def _args(scenario: str) -> dict:
    return {"objective": "ход", "reason": "нужно", "scenarios": [scenario]}


class AnsweredGoal:
    """`aifa_goal` отвечает `ГОТОВО`: стандартный путь справился, цикл не открыт."""

    async def __call__(self, record, emit) -> GoalResult:
        return GoalResult(Verdict.ANSWERED, "Расчёт выполнен.")


async def test_the_cycle_is_opened_only_by_the_answer_that_needs_it(base) -> None:
    """LR-29, C3.9: после `ГОТОВО` `aifa_move`, `aifa_finish` и `aifa_state` без id — `ОТКАЗ нет_цикла`; чтение по id и
    `aifa_doc` отвечают."""
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=())
    plugin = Plugin(ToolApi(Gateway(), AnsweredGoal()))
    ctx = ctx_of(run.run_id)

    assert first_line((await plugin.call("aifa_goal", ctx))) == "ГОТОВО"
    refused = [
        first_line(await plugin.call(tool, ctx, args))
        for tool, args in (("aifa_move", _args("а")), ("aifa_finish", {"action": "give_up"}), ("aifa_state", {}))
    ]
    doc = await plugin.call("aifa_doc", ctx)

    assert refused == ["ОТКАЗ нет_цикла"] * 3
    assert first_line(doc) == "ГОТОВО"
    await plugin.close()
    await run.end()


async def test_nothing_of_the_cycle_is_readable_before_the_goal_was_asked(base) -> None:
    """LR-30, §3.0: `aifa_state(artifact_ids)` и `aifa_doc` доступны после любого `aifa_goal`, не раньше."""
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=())
    plugin = Plugin(ToolApi(Gateway(), AnsweredGoal()))
    ctx = ctx_of(run.run_id)

    state = await plugin.call("aifa_state", ctx, {"artifact_ids": ["x"]})
    doc = await plugin.call("aifa_doc", ctx)

    assert first_line(state) == first_line(doc) == "ОТКАЗ нет_цикла"
    await plugin.close()
    await run.end()


def first_line_of(reply: str) -> str:
    return reply.split("\n", 1)[0]

