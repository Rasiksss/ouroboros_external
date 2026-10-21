"""Изменяющие инструменты цикла `aifa_move` и `aifa_finish` (CONTRACTS.md §3.1, ADR-0042, ADR-0060)."""

from __future__ import annotations

import asyncio
from typing import Any

from finengine.assistant import ouro_view
from finengine.assistant.goal_tools import reply_of
from finengine.assistant.jobs import JobBoard
from finengine.assistant.loop_desk import LoopDesk, OpenLoop
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_call import BoundCall
from finengine.config import settings
from finengine.skills.production_runtime import publish_result
from finengine.skills.spec.dynamic_run import Rejection, decision_from_data

FINISH_OBJECTIVE = "Ответить пользователю"
FINISH_REASON = "Цикл завершает ouroboros"


def no_loop(why: str = "") -> str:
    return ouro_view.refusal(Refusal.NO_LOOP, why or "Цикл не открыт: его открывает aifa_open.")


class ExclusiveTool:
    """Вызов, меняющий `LoopSession`: идёт по одному на ход, ожидание очереди входит в срок вызова (ADR-0043 п. 4)."""

    name = ""
    root_only = False

    def __init__(self, board: JobBoard, desk: LoopDesk) -> None:
        self._board = board
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        loop = await self._desk.open_loop_of(call)
        if loop is None:
            return no_loop()
        job = self._board.start(call.ctx.run_id, self.name, self._queued(loop, call.args))
        return await reply_of(self._board, job, settings.ouroboros_goal_wait_sec)

    async def _queued(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        async with loop.queue:
            return await asyncio.to_thread(self._execute, loop, args)

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        raise NotImplementedError

    def _rejected(self, loop: OpenLoop, reason: Rejection) -> str:
        """Отказ хода не тратит, но идёт в счёт отказов подряд: три подряд — `ЛИМИТ` (ADR-0060 п. 5)."""
        loop.session.reject(reason, feedback=f"Твой вызов {self.name} не принят: {reason}.", spends_move=False)
        self._desk.save(loop.run_id)
        return ouro_view.refusal(Refusal(reason.code), reason)


class MoveTool(ExclusiveTool):
    name = "aifa_move"
    root_only = True    # ходы тратят общие пределы цикла: подзадача их не ведёт (ADR-0060 п. 5)

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        session = loop.session
        if session.closed:
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Цикл закрыт aifa_finish: ходить больше нельзя.")
        reason = session.limit()
        if reason is not None:
            return ouro_view.limited(reason)
        try:
            decision = decision_from_data(
                {**args, "action": "execute"}, scenarios_enabled=False,
                goals_enabled=session.run_goals is not None, raw_query=session.goal.raw_query,
            )
        except ValueError as problem:
            return self._rejected(loop, Rejection(str(problem)))
        checked = session.check(decision)
        if isinstance(checked, Rejection):
            return self._rejected(loop, checked)
        known = ouro_view.shown_ids(session)
        outcome = session.move(checked)
        self._desk.save(loop.run_id)
        if outcome.refusal:
            return ouro_view.refusal(Refusal(outcome.refusal), outcome.details)
        return ouro_view.moved(
            session, outcome, known, limit=settings.ouroboros_result_limit, exhausted=session.limit(),
        )


class FinishTool(ExclusiveTool):
    name = "aifa_finish"
    root_only = True

    def __init__(self, board: JobBoard, desk: LoopDesk, subtasks: SubtaskWaits) -> None:
        super().__init__(board, desk)
        self._subtasks = subtasks

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        session = loop.session
        if session.closed:
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Цикл уже закрыт: aifa_finish принят раньше.")
        try:
            decision = decision_from_data(
                {
                    "objective": FINISH_OBJECTIVE, "reason": FINISH_REASON, "action": args.get("action"),
                    "deliverables": args.get("deliverables", []), "evidence_ids": args.get("evidence_ids", []),
                },
                scenarios_enabled=False, answer_in_chat=True,
            )
        except ValueError as problem:
            return self._rejected(loop, Rejection(str(problem)))
        last_word = session.limit() is not None
        checked = session.check(decision, last_word=last_word)
        if isinstance(checked, Rejection) and not last_word:
            return self._rejected(loop, checked)
        # Последнее слово не принято: цикл конечен — закрывается сводкой и показывает найденное, как у цикла службы.
        if isinstance(checked, Rejection):
            result, refused = session.stopped(checked), str(checked)
        else:
            result, refused = session.finish(checked), ""
        self._desk.save(loop.run_id)
        publish_result(result, session.event_sink, case_id=session.case_id, document_id=loop.origin.document_id)
        return ouro_view.accepted(result, reason=refused, unfinished=self._subtasks.unfinished(loop.run_id))
