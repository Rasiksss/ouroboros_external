"""Инструменты подзадач `aifa_register_atom` и `aifa_report` (CONTRACTS.md §3.1, ADR-0041 п. 4, ADR-0042 п. 1).

Общие операции над данными службы: ими пользуется любая подзадача любого навыка, служба навыка не знает. Проверку и
регистрацию атома делает `atom_intake` (через `LoopSession.register_atom`), здесь только очередь хода и перевод исхода
в ответ (`ouro_view`).
"""

from __future__ import annotations

from typing import Any

from finengine.assistant import ouro_view
from finengine.assistant.loop_desk import LoopDesk, OpenLoop
from finengine.assistant.loop_tools import ExclusiveTool, no_loop
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.subtask_waits import SubtaskWaits
from finengine.assistant.tool_call import BoundCall
from finengine.skills.spec.atom_intake import Refused, StationDown
from finengine.skills.spec.dynamic_run import LoopSession, parse_atom_request

LABEL_MAX = 80
NOTE_MAX = 500
STATUSES = ("ok", "error", "done")


class RegisterTool(ExclusiveTool):
    """Сдача атома с кодом: изменяющий вызов, идёт в очереди хода, «240 с, затем фон» (ADR-0043 п. 4)."""

    name = "aifa_register_atom"

    def _execute(self, loop: OpenLoop, args: dict[str, Any]) -> str:
        session: LoopSession = loop.session
        if session.closed:
            return ouro_view.refusal(Refusal.LOOP_CLOSED, "Цикл закрыт aifa_finish: регистрировать атомы больше нельзя.")
        reason = session.limit()
        if reason is not None:
            return ouro_view.limited(reason)
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "code — исходный текст с функцией run(inputs).")
        if args.get("url"):
            return ouro_view.refusal(
                Refusal.BAD_ARGUMENTS, "url не принимается: атом-загрузка описывается в new_atoms хода aifa_move."
            )
        try:
            request = parse_atom_request({key: value for key, value in args.items() if key != "code"}, "аргументы")
        except ValueError as problem:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, str(problem))
        outcome = session.register_atom(request, code)
        self._desk.save(loop.run_id)
        if isinstance(outcome, Refused):
            return ouro_view.refusal(Refusal(outcome.code), outcome.why)
        if isinstance(outcome, StationDown):
            return ouro_view.service_fault(f"комната станции недоступна, код не проверить ({outcome.why})")
        return ouro_view.registered(request.name, outcome.atom.version, outcome.output)


class ReportTool:
    """Отчёт подзадачи. `ok`/`error` — попытка в ленту «Планировщик и автор»; `done` — итог подзадачи, только из
    подзадачи, без события (ADR-0042 п. 1). Вне очереди: меняет только счётчик попыток (ADR-0044 п. 4)."""

    name = "aifa_report"
    root_only = False

    def __init__(self, desk: LoopDesk, subtasks: SubtaskWaits) -> None:
        self._desk = desk
        self._subtasks = subtasks

    async def run(self, call: BoundCall) -> str:
        raw_label, status, raw_note, raw_code = (call.args.get(key) for key in ("label", "status", "note", "code"))
        problem = _problem_in(raw_label, status, raw_note, raw_code)
        if problem:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, problem)
        label, note, code = str(raw_label), str(raw_note or ""), str(raw_code or "")
        run_id, task_id = call.ctx.run_id, str(call.ctx.task_id)
        if status == "done":
            if call.is_root:
                return ouro_view.refusal(Refusal.NOT_THE_ROLE, "aifa_report со status=done вызывает только подзадача.")
            self._subtasks.report_done(run_id, task_id, note or label)
            return f"{ouro_view.Code.READY}\nИтог принят: корень получит его в aifa_wait."
        loop = await self._desk.open_loop_of(call)
        if loop is None:
            return no_loop()
        if not self._subtasks.count_report(run_id, task_id):
            return ouro_view.refusal(
                Refusal.REPORTS_LIMIT, "Не больше 20 отчётов на задачу: заверши работу и сдай итог status=done."
            )
        # Счётчик уезжает в хранилище с ближайшим сохранением цикла (ход, регистрация): сохранять отсюда — вне очереди —
        # значило бы гоняться со снимком идущего хода.
        loop.session.intake.report(label, str(status), note if status == "error" else None, code)
        return f"{ouro_view.Code.READY}\nОтчёт записан в ленту хода."


def _problem_in(label: Any, status: Any, note: Any, code: Any) -> str:
    """Что не так с аргументами `aifa_report`; пусто — всё так."""
    if not isinstance(label, str) or not label.strip() or len(label) > LABEL_MAX:
        return f"label — непустая строка не длиннее {LABEL_MAX} знаков (имя атома или шага)."
    if status not in STATUSES:
        return "status — ok, error или done."
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        return f"note — строка не длиннее {NOTE_MAX} знаков."
    if code is not None and not isinstance(code, str):
        return "code — строка с исходным текстом."
    return ""

