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
from finengine.assistant.ouro_view import Refusal
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
    """Итог задания, `ЖДИ`, если не успело за `seconds`, или отказ, если ход закончился."""
    try:
        result = await board.wait(job, seconds)
    except RunEnded:
        return ouro_view.refusal(Refusal.FOREIGN_RUN, "Ход уже закончен или остановлен.")
    if result is None:
        return ouro_view.waiting(job.job_id)
    return ouro_view.goal(result, limit=settings.ouroboros_result_limit)


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
            job = self._board.start(run_id, GOAL_KIND, self._goal(call.record, RunEmitter(run_id)))
        return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)


class WaitTool:
    name = "aifa_wait"
    root_only = False

    def __init__(self, board: JobBoard) -> None:
        self._board = board

    async def run(self, call: BoundCall) -> str:
        problem = _problem_in(call.args)
        if problem:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, problem)
        job = self._board.find(call.ctx.run_id, call.args["job_id"])
        if job is None:
            return ouro_view.refusal(Refusal.JOB_NOT_FOUND, "Задания с таким job_id в этом ходе нет.")
        return await reply_of(self._board, job, float(call.args["wait_sec"]))


def _problem_in(args: dict[str, Any]) -> str:
    """Что не так с аргументами `aifa_wait`; пусто — всё так."""
    if "task_id" in args:
        return "Ожидание подзадачи по task_id в этой версии не поддерживается: подзадач нет."
    job_id, wait_sec = args.get("job_id"), args.get("wait_sec")
    if not isinstance(job_id, str) or not job_id:
        return "Нужен job_id — из ответа «ЖДИ job=<id>»."
    if isinstance(wait_sec, bool) or not isinstance(wait_sec, int | float):
        return f"Нужен wait_sec — число секунд от {WAIT_MIN_SEC} до {WAIT_MAX_SEC}."
    if not WAIT_MIN_SEC <= wait_sec <= WAIT_MAX_SEC:
        return f"wait_sec вне пределов: от {WAIT_MIN_SEC} до {WAIT_MAX_SEC} секунд."
    return ""
