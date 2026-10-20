"""Инструменты `aifa_goal` и `aifa_wait` (CONTRACTS.md §3.1, ADR-0042 п. 1).

Длинное — фоном: `aifa_goal` держит вызов до `ouroboros_goal_wait_sec` (240 с), затем отвечает
`ЖДИ job=<id>`; дальше — `aifa_wait`, который держит запрос столько, сколько просили (`wait_sec`).
Срок держит служба; плагин ждёт один запрос и между опросами проверяет остановку (ADR-0022 п. 3, P61).
"""

from __future__ import annotations

from typing import Any, Protocol

from finengine.assistant import ouro_view
from finengine.assistant.events import Emit, Event
from finengine.assistant.goal_result import GoalResult
from finengine.assistant.jobs import Job, JobBoard, RunEnded
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.subtask_waits import SubtaskWaits, Verdict
from finengine.assistant.tool_call import BoundCall
from finengine.assistant.turn_record import TurnRecord
from finengine.config import settings
from finengine.skills import run_orchestrator

WAIT_MIN_SEC = 10
WAIT_MAX_SEC = 240
GOAL_KIND = "aifa_goal"


class GoalSource(Protocol):
    """Откуда берётся итог цели хода: стандартный путь службы (шаг 09)."""

    async def __call__(self, record: TurnRecord, emit: Emit) -> GoalResult: ...


class RunEmitter:
    """События конвейера — в журнал этого хода, на любой реплике (`emit_to_run`)."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id

    def __call__(self, event: Event) -> None:
        run_orchestrator.emit_to_run(self._run_id, event)


async def reply_of(board: JobBoard, job: Job, seconds: float) -> str:
    """Ответ задания, `ЖДИ`, если не успело за `seconds`, или отказ, если ход закончился."""
    try:
        text = await board.wait(job, seconds)
    except RunEnded:
        return ouro_view.refusal(Refusal.FOREIGN_RUN, "Ход уже закончен или остановлен.")
    return ouro_view.waiting(job.job_id) if text is None else text


class GoalTool:
    name = GOAL_KIND
    root_only = True

    def __init__(self, board: JobBoard, goal: GoalSource) -> None:
        self._board = board
        self._goal = goal

    async def run(self, call: BoundCall) -> str:
        run_id = call.ctx.run_id
        # C3.3: повторный вызов в ходе — то же задание и тот же итог, цель заново не разбирается.
        job = self._board.of_kind(run_id, GOAL_KIND)
        if job is None:
            job = self._board.start(run_id, GOAL_KIND, self._answer(call))
        return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)

    async def _answer(self, call: BoundCall) -> str:
        result = await self._goal(call.record, RunEmitter(call.ctx.run_id))
        return ouro_view.goal(result, limit=settings.ouroboros_result_limit)


class WaitTool:
    """Длинный опрос: итог задания службы (`job_id`) или подзадачи ouroboros (`task_id`, ADR-0041 п. 3)."""

    name = "aifa_wait"
    root_only = False

    def __init__(self, board: JobBoard, subtasks: SubtaskWaits, desk: LoopDesk) -> None:
        self._board = board
        self._subtasks = subtasks
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        problem = _problem_in(call.args)
        if problem:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, problem)
        if "task_id" in call.args:
            return await self._subtask(call)
        job = self._board.find(call.ctx.run_id, call.args["job_id"])
        if job is None:
            return ouro_view.refusal(Refusal.JOB_NOT_FOUND, "Задания с таким job_id в этом ходе нет.")
        return await reply_of(self._board, job, float(call.args["wait_sec"]))

    async def _subtask(self, call: BoundCall) -> str:
        """Срок подзадачи — от первого опроса и не дольше времени цикла; вне цикла — только срок (ADR-0044 п. 5)."""
        task_id = call.args["task_id"]
        loop = await self._desk.open_loop_of(call)
        cycle_ends_at = None if loop is None else loop.session.opened_at + loop.session.limits.max_wall_seconds
        try:
            waited = await self._subtasks.wait(
                call.ctx.run_id, task_id, float(call.args["wait_sec"]), cycle_ends_at=cycle_ends_at
            )
        except RunEnded:
            return ouro_view.refusal(Refusal.FOREIGN_RUN, "Ход уже закончен или остановлен.")
        if waited.outcome is not None:
            return ouro_view.subtask_result(
                task_id, waited.outcome.text, failed=waited.outcome.failed, limit=settings.ouroboros_result_limit
            )
        if waited.verdict is Verdict.SILENT:
            return ouro_view.subtask_silent(task_id, settings.ouroboros_subtask_wait_sec)
        if waited.verdict is Verdict.OUT_OF_TIME:
            return ouro_view.limited("исчерпан бюджет времени цикла: подзадачу не дождались")
        return ouro_view.waiting_subtask(task_id)


def _problem_in(args: dict[str, Any]) -> str:
    """Что не так с аргументами `aifa_wait`; пусто — всё так."""
    job_id, task_id, wait_sec = args.get("job_id"), args.get("task_id"), args.get("wait_sec")
    if "job_id" in args and "task_id" in args:
        return "Нужен либо job_id, либо task_id, не оба."
    if "task_id" in args:
        if not isinstance(task_id, str) or not task_id:
            return "task_id — id подзадачи из ответа schedule_task."
    elif not isinstance(job_id, str) or not job_id:
        return "Нужен job_id — из ответа «ЖДИ job=<id>» — или task_id подзадачи."
    if isinstance(wait_sec, bool) or not isinstance(wait_sec, int | float):
        return f"Нужен wait_sec — число секунд от {WAIT_MIN_SEC} до {WAIT_MAX_SEC}."
    if not WAIT_MIN_SEC <= wait_sec <= WAIT_MAX_SEC:
        return f"wait_sec вне пределов: от {WAIT_MIN_SEC} до {WAIT_MAX_SEC} секунд."
    return ""
