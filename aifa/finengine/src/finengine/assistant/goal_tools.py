"""Инструменты `aifa_open` и `aifa_wait` (CONTRACTS.md §3.1, ADR-0060 п. 1).

Длинное — фоном: `aifa_open` держит вызов до `ouroboros_goal_wait_sec` (240 с), затем отвечает
`ЖДИ job=<id>`; дальше — `aifa_wait`, который держит запрос столько, сколько просили (`wait_sec`).
Срок держит служба; плагин ждёт один запрос и между опросами проверяет остановку (ADR-0022 п. 3, P61).
"""

from __future__ import annotations

import asyncio
import uuid
import time
from typing import Any, Protocol

from finengine.assistant import ouro_view
from finengine.assistant.events import Event
from finengine.assistant.jobs import Job, JobBoard, RunEnded
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_opening import NoDocument
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.subtask_waits import SubtaskWaits, Verdict
from finengine.assistant.tool_call import BoundCall
from finengine.assistant.turn_record import TurnRecord
from finengine.config import settings
from finengine.skills import run_orchestrator
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.store import state

WAIT_MIN_SEC = 10
WAIT_MAX_SEC = 240
OPEN_KIND = "aifa_open"


class Opening(Protocol):
    """Кто открывает цикл хода: служба по документу хода (`LoopOpening`)."""

    async def __call__(self, record: TurnRecord) -> LoopSession | NoDocument: ...

    async def market(self, record: TurnRecord, agent_id: str = "") -> LoopSession: ...


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


class OpenTool:
    name = OPEN_KIND
    root_only = True

    def __init__(self, board: JobBoard, opening: Opening, desk: LoopDesk) -> None:
        self._board = board
        self._opening = opening
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        mode = call.args.get("mode", "report")
        if mode not in ("report", "market"):
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "mode — report или market")
        existing = await self._desk.open_loop_of(call)
        if existing is not None and existing.origin.mode != mode:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "Режим уже открытого цикла менять нельзя")
        run_id = call.ctx.run_id
        # C3.3: повторный вызов в ходе — то же задание; открытый цикл не открывается заново, ответ — его состояние.
        kind = OPEN_KIND if mode == "report" else f"{OPEN_KIND}:market"
        job = self._board.of_kind(run_id, kind)
        if job is None:
            job = self._board.start(run_id, kind, self._answer(call))
        return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)

    async def _answer(self, call: BoundCall) -> str:
        mode = call.args.get("mode", "report")
        token = uuid.uuid4().hex
        waiting, pause = False, 0.05
        while True:
            loop = await self._desk.open_loop_of(call)
            if loop is not None:
                if loop.origin.mode != mode:
                    return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "Режим уже открытого цикла менять нельзя")
                return ouro_view.opened(loop.session, limit=settings.ouroboros_result_limit)
            # Заявку пишем один раз; ожидающий только читает её, пока открытие не закончится.
            if waiting:
                claim = await asyncio.to_thread(state.get, "loop_opening", owner=call.record.user_id, key=call.ctx.run_id)
                if claim is None:
                    waiting = False
                    continue
            else:
                claim = await asyncio.to_thread(
                    state.claim, "loop_opening", owner=call.record.user_id, key=call.ctx.run_id,
                    doc={"mode": mode, "token": token, "expires_at": time.time() + 300},
                )
            if claim.get("expires_at", 0) < time.time():
                return ouro_view.refusal(Refusal.JOB_NOT_FOUND, "Открытие прервано или превышен срок; начни новый прогон")
            if claim["mode"] != mode:
                return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "Открытие другого режима уже принято")
            if claim["token"] == token:
                try:
                    return await self._open_new(call)
                finally:
                    if self._desk.live(call.ctx.run_id) is None:
                        await asyncio.to_thread(state.delete_one, "loop_opening", owner=call.record.user_id, key=call.ctx.run_id)
            waiting = True
            await asyncio.sleep(pause)
            pause = min(pause * 2, 1.0)

    async def _open_new(self, call: BoundCall) -> str:
        opened: LoopSession | NoDocument
        if call.args.get("mode", "report") == "market":
            opened = await self._opening.market(call.record, call.ctx.agent_id)
        else:
            opened = await self._opening(call.record)
        if isinstance(opened, NoDocument):
            return ouro_view.refusal(Refusal.NO_DOCUMENT, opened.why)
        return ouro_view.opened(opened, limit=settings.ouroboros_result_limit)


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
