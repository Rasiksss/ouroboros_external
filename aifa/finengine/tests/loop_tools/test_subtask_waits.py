"""Подзадачи хода: `aifa_wait(task_id)`, `aifa_report(done)`, роли и срок (CONTRACTS.md §3.1, §4, ADR-0041 п. 3,
ADR-0043 пп. 2–3, 8, ADR-0044 п. 5).

Подзадачу играет вызов с `task_depth=1`; кадр её итога — `SubtaskWaits.subtask_finished`, как его зовёт шлюз хода
(`turn_gateway`, C2.3–C2.4). Служба о навыке подзадачи ничего не знает: ни одного имени навыка в этих тестах нет.
"""
from __future__ import annotations

import asyncio

from loop_support import Cycle, Gateway, _NoGoal, open_cycle
from test_dynamic_run import ExtractScenario, _move
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_api import ToolApi
from finengine.assistant.turn_gateway import SubtaskResult
from finengine.config import settings
from finengine.skills.spec.dynamic_run import RunLimits

SUB = "sub-task"
FAILURE = "⚠️ Error during processing: сбой"


def first_line_of(text: str) -> str:
    return text.split("\n", 1)[0]


def done_frame(cycle: Cycle, text: str = "Итог подзадачи.", *, task: str = SUB, run_id: str | None = None):
    return SubtaskResult(run_id or cycle.run.run_id, task, text, failed=text.startswith(FAILURE[:20]))


async def waiting_cycle(base, waits: SubtaskWaits, **kwargs) -> Cycle:
    return await open_cycle(base, scenario=ExtractScenario(base, None), subtasks=waits, **kwargs)


async def test_the_frame_of_a_subtask_result_ends_the_wait_of_the_root(base) -> None:
    """C4.6: `aifa_wait(task_id)` завершается кадром итога подзадачи; планировщик получает его текст."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    waiting = asyncio.create_task(cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 5}))
    await asyncio.sleep(0.2)

    waits.subtask_finished(done_frame(cycle))
    reply = await asyncio.wait_for(waiting, timeout=3)

    assert first_line_of(reply) == "ГОТОВО" and "Итог подзадачи." in reply
    await cycle.close()


async def test_a_done_report_of_the_subtask_ends_the_wait_with_its_note(base) -> None:
    """C4.6: отдельно — `aifa_report(status="done")` из подзадачи; итог — её `note`; событий экрана нет."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    waiting = asyncio.create_task(cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 5}))
    await asyncio.sleep(0.2)

    reported = await cycle.call("aifa_report", {"label": "итог", "status": "done", "note": "Атом принят."}, depth=1)
    reply = await asyncio.wait_for(waiting, timeout=3)

    assert first_line_of(reported) == "ГОТОВО"
    assert first_line_of(reply) == "ГОТОВО" and "Атом принят." in reply
    assert [e for e in cycle.events if e["type"] == "author_attempt"] == []
    await cycle.close()


async def test_a_failed_subtask_is_an_outcome_the_planner_can_read(base) -> None:
    """ADR-0043 п. 3: кадр «⚠️ Error during processing…» — итог «подзадача упала», а не зависшее ожидание."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    waits.subtask_finished(SubtaskResult(cycle.run.run_id, SUB, FAILURE, failed=True))

    reply = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 5})

    assert first_line_of(reply) == "ГОТОВО" and "подзадача упала" in reply and "сбой" in reply
    await cycle.close()


async def test_the_frame_of_another_run_of_the_same_chat_does_not_end_the_wait(base, monkeypatch) -> None:
    """C4.9: кадр чужого хода того же чата ожидание не завершает."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    waits.subtask_finished(done_frame(cycle, run_id="another-run"))

    reply = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 0.2})

    assert first_line_of(reply) == f"ЖДИ task={SUB}"
    await cycle.close()


async def test_every_wait_that_goes_on_says_how_to_look_at_the_task_or_to_cancel_it(base) -> None:
    """ADR-0043 п. 3: каждое `ЖДИ` по `task_id` — с подсказкой `get_task_result`/`cancel_task`."""
    cycle = await waiting_cycle(base, SubtaskWaits())

    reply = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 0.1})

    assert first_line_of(reply) == f"ЖДИ task={SUB}"
    assert "get_task_result" in reply and "cancel_task" in reply and "aifa_wait" in reply
    await cycle.close()


async def test_a_silent_subtask_is_refused_by_its_term_counted_from_the_first_wait(base, monkeypatch) -> None:
    """C4.9: молчащая (или отменённая — кадра нет) подзадача — `подзадача_не_ответила` по сроку; срок идёт от первого `aifa_wait`."""
    monkeypatch.setattr(settings, "ouroboros_subtask_wait_sec", 0.4)
    cycle = await waiting_cycle(base, SubtaskWaits())

    first = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 0.2})
    await asyncio.sleep(0.4)
    second = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 0.2})

    assert first_line_of(first) == f"ЖДИ task={SUB}"
    assert first_line_of(second) == "ОТКАЗ подзадача_не_ответила"
    assert "get_task_result" in second and "cancel_task" in second
    await cycle.close()


async def test_the_time_of_the_cycle_cuts_the_term_of_a_subtask_short(base) -> None:
    """C4.9: срок подзадачи не дольше остатка времени цикла — тогда `ЛИМИТ`."""
    cycle = await waiting_cycle(base, SubtaskWaits(), limits=RunLimits(max_wall_seconds=0.3))

    reply = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 5})

    assert first_line_of(reply) == "ЛИМИТ"
    await cycle.close()


async def test_outside_a_cycle_only_the_term_of_the_subtask_counts(monkeypatch) -> None:
    """ADR-0044 п. 5, C4.9: вне цикла `ЛИМИТ` по времени цикла невозможен — только срок подзадачи."""
    monkeypatch.setattr(settings, "ouroboros_subtask_wait_sec", 0.3)
    run = open_run()
    plugin = Plugin(ToolApi(Gateway(), _NoGoal(), LoopDesk(), SubtaskWaits()))

    reply = await plugin.call("aifa_wait", ctx_of(run.run_id), {"task_id": SUB, "wait_sec": 5})

    assert first_line(reply) == "ОТКАЗ подзадача_не_ответила"
    await plugin.close()
    await run.end()


async def test_a_finished_subtask_is_answered_even_when_the_cycle_time_is_gone(base) -> None:
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits, limits=RunLimits(max_wall_seconds=0.0))
    waits.subtask_finished(done_frame(cycle))

    reply = await cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 5})

    assert first_line_of(reply) == "ГОТОВО"
    await cycle.close()


async def test_a_wait_names_either_a_job_or_a_subtask(base) -> None:
    cycle = await waiting_cycle(base, SubtaskWaits())

    both = await cycle.call("aifa_wait", {"job_id": "j", "task_id": SUB, "wait_sec": 10})
    neither = await cycle.call("aifa_wait", {"wait_sec": 10})

    assert first_line_of(both) == "ОТКАЗ неверные_аргументы" and first_line_of(neither) == "ОТКАЗ неверные_аргументы"
    await cycle.close()


async def test_done_is_for_a_subtask_only_and_the_root_is_not_the_role(base) -> None:
    """LR-54, C3.0, C4.8: `aifa_report(status="done")` из корня — `ОТКАЗ не_та_роль`."""
    cycle = await waiting_cycle(base, SubtaskWaits())

    reply = await cycle.call("aifa_report", {"label": "итог", "status": "done", "note": "готово"})

    assert first_line_of(reply) == "ОТКАЗ не_та_роль"
    await cycle.close()


async def test_a_subtask_may_report_done_without_an_open_cycle() -> None:
    """Конец подзадачи от цикла не зависит: `done` не нужен цикл."""
    waits = SubtaskWaits()
    run = open_run()
    plugin = Plugin(ToolApi(Gateway(), _NoGoal(), LoopDesk(), waits))
    ctx = ctx_of(run.run_id, task_id=SUB, root_task_id="root-task", task_depth=1)

    reply = await plugin.call("aifa_report", ctx, {"label": "итог", "status": "done", "note": "ничего не нужно"})

    assert first_line(reply) == "ГОТОВО"
    assert waits.unfinished(run.run_id) == []
    await plugin.close()
    await run.end()


async def test_reports_of_one_task_are_limited_but_done_is_always_taken(base) -> None:
    """LR-57, ADR-0042 «Следствия»: ≤ 20 отчётов на задачу; `done` не считается, иначе подзадача не смогла бы закончиться."""
    cycle = await waiting_cycle(base, SubtaskWaits())
    await cycle.move(_move(scenarios=("ещё",)))

    for number in range(20):
        reply = await cycle.call("aifa_report", {"label": "eph.x", "status": "ok", "note": str(number)}, depth=1)
        assert first_line_of(reply) == "ГОТОВО"
    twenty_first = await cycle.call("aifa_report", {"label": "eph.x", "status": "ok", "note": "лишний"}, depth=1)
    other_task = await cycle.call("aifa_report", {"label": "eph.x", "status": "ok", "note": "я"}, depth=1, task="other")
    done = await cycle.call("aifa_report", {"label": "итог", "status": "done", "note": "всё"}, depth=1)

    assert first_line_of(twenty_first) == "ОТКАЗ лимит_отчётов"
    assert first_line_of(other_task) == "ГОТОВО" and first_line_of(done) == "ГОТОВО"
    await cycle.close()


async def test_the_subtask_is_remembered_by_its_first_call_and_forgotten_when_it_finishes(base) -> None:
    """ADR-0041 п. 3: подзадачу хода служба запоминает по первому её вызову; кадр итога снимает её с учёта."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    assert waits.unfinished(cycle.run.run_id) == []

    await cycle.call("aifa_state", depth=1, task="child-a")
    await cycle.call("aifa_state", depth=1, task="child-b")
    assert sorted(waits.unfinished(cycle.run.run_id)) == ["child-a", "child-b"]

    waits.subtask_finished(done_frame(cycle, task="child-a"))
    assert waits.unfinished(cycle.run.run_id) == ["child-b"]
    await cycle.close()


async def test_the_accepted_finish_lists_the_subtasks_the_service_has_not_seen_finished(base) -> None:
    """ADR-0043 п. 8, C4.1: `ПРИНЯТО` перечисляет незавершённые подзадачи хода — снять `cancel_task`."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    await cycle.call("aifa_state", depth=1, task="child-a")
    await cycle.call("aifa_state", depth=1, task="child-b")
    waits.subtask_finished(done_frame(cycle, task="child-a"))

    reply = await cycle.call("aifa_finish", {"action": "give_up"})

    assert first_line_of(reply) == "ПРИНЯТО"
    assert "child-b" in reply and "cancel_task" in reply and "child-a" not in reply
    await cycle.close()


async def test_a_subtask_that_calls_a_tool_of_another_run_is_refused(base) -> None:
    """C4.1, C3.0: вызовы подзадачи принимаются только в её ходе."""
    cycle = await waiting_cycle(base, SubtaskWaits())

    reply = await cycle.call("aifa_state", depth=1, task="child-a")
    other = await Plugin(ToolApi(Gateway(), _NoGoal(), LoopDesk())).call(
        "aifa_state", ctx_of("unknown-run", task_id="child-a", root_task_id="root-task", task_depth=1)
    )

    assert first_line_of(reply) == "ГОТОВО"
    assert first_line(other) == "ОТКАЗ чужой_ход"
    await cycle.close()


async def test_the_end_of_the_run_ends_the_wait_and_the_service_forgets_the_run(base) -> None:
    """Нет утечки ожиданий: ход закончился — `aifa_wait` отвечает отказом, а подзадачи хода забываются."""
    waits = SubtaskWaits()
    cycle = await waiting_cycle(base, waits)
    waiting = asyncio.create_task(cycle.call("aifa_wait", {"task_id": SUB, "wait_sec": 30}))
    await asyncio.sleep(0.3)

    await cycle.run.end()
    reply = await asyncio.wait_for(waiting, timeout=5)
    waits.remember("another-run", "another-task")

    assert first_line_of(reply) == "ОТКАЗ чужой_ход"
    assert waits.unfinished(cycle.run.run_id) == [], "подзадачи закончившегося хода забыты"
    await cycle.plugin.close()
