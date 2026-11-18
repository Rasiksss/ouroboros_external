"""Подзадачи хода: какие закончили и сколько их ждать (CONTRACTS.md §3.1, §4; ADR-0041 п. 3, ADR-0043 пп. 3, 8).

Служба не знает, какой навык у подзадачи: она видит задачу ouroboros хода — по её первому вызову инструмента, по
кадру её итога или по `aifa_wait(task_id)` корня. Подзадача закончена по первому из двух: кадр итога (шлюз хода,
§2.3) или её `aifa_report(status="done")`.

Состояние — в памяти процесса, как у `JobBoard`: служба пока одна реплика (`--workers 1`); кадр итога приходит туда,
где идёт ход, туда же приходят и вызовы инструментов.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic

from finengine.assistant.jobs import RunEnded
from finengine.assistant.turn_gateway import SubtaskResult
from finengine.assistant.turn_record import run_is_active
from finengine.config import settings

# Отчётов `ok`/`error` на задачу ouroboros (ADR-0042 «Следствия»); `done` не считается: подзадача, исчерпавшая
# отчёты, всё равно должна уметь закончиться.
REPORTS_PER_TASK = 20
# Как часто ожидание сверяет состояние: кадр итога приходит из другой корутины.
POLL_SEC = 0.05


@dataclass(frozen=True)
class SubtaskOutcome:
    text: str
    failed: bool = False


class Verdict(StrEnum):
    DONE = "done"
    GOING = "going"
    SILENT = "silent"
    OUT_OF_TIME = "out_of_time"


@dataclass(frozen=True)
class Waited:
    verdict: Verdict
    outcome: SubtaskOutcome | None = None


@dataclass
class _Subtask:
    outcome: SubtaskOutcome | None = None
    first_wait: float | None = None
    reports: int = 0


@dataclass
class SubtaskWaits:
    """Реестр подзадач ходов; он же `SubtaskSink` шлюза хода."""

    _runs: dict[str, dict[str, _Subtask]] = field(default_factory=dict)

    def remember(self, run_id: str, task_id: str) -> None:
        self._sweep(keep=run_id)
        self._runs.setdefault(run_id, {}).setdefault(task_id, _Subtask())

    def subtask_finished(self, result: SubtaskResult) -> None:
        """Кадр итога подзадачи (§2.3); «⚠️ Error during processing…» — итог «подзадача упала»."""
        self.remember(result.run_id, result.task_id)
        self._runs[result.run_id][result.task_id].outcome = SubtaskOutcome(result.text, result.failed)

    def report_done(self, run_id: str, task_id: str, note: str) -> None:
        self.remember(run_id, task_id)
        self._runs[run_id][task_id].outcome = SubtaskOutcome(note)

    def count_report(self, run_id: str, task_id: str) -> bool:
        """Принять отчёт `ok`/`error` задачи; `False` — предел отчётов исчерпан."""
        self.remember(run_id, task_id)
        subtask = self._runs[run_id][task_id]
        if subtask.reports >= REPORTS_PER_TASK:
            return False
        subtask.reports += 1
        return True

    def unfinished(self, run_id: str) -> list[str]:
        return [task_id for task_id, subtask in self._runs.get(run_id, {}).items() if subtask.outcome is None]

    async def wait(self, run_id: str, task_id: str, seconds: float, *, cycle_ends_at: float | None) -> Waited:
        """Длинный опрос подзадачи. Срок — от первого опроса по ней, не дольше конца времени цикла
        (`cycle_ends_at`, монотонные часы; `None` — цикла нет, ADR-0044 п. 5). Ход закончился — `RunEnded`."""
        self.remember(run_id, task_id)
        subtask = self._runs[run_id][task_id]
        if subtask.first_wait is None:
            subtask.first_wait = monotonic()
        term_ends_at = subtask.first_wait + settings.ouroboros_subtask_wait_sec
        limited = cycle_ends_at is not None and cycle_ends_at < term_ends_at
        ends_at = cycle_ends_at if limited and cycle_ends_at is not None else term_ends_at
        hold_until = min(monotonic() + seconds, ends_at)
        while subtask.outcome is None and monotonic() < hold_until:
            if not run_is_active(run_id):
                raise RunEnded(run_id)
            await asyncio.sleep(min(POLL_SEC, max(hold_until - monotonic(), 0)))
        if subtask.outcome is not None:
            return Waited(Verdict.DONE, subtask.outcome)
        if monotonic() >= ends_at:
            return Waited(Verdict.OUT_OF_TIME if limited else Verdict.SILENT)
        return Waited(Verdict.GOING)

    def _sweep(self, *, keep: str) -> None:
        """Забыть подзадачи закончившихся ходов; без этого словарь рос бы до перезапуска."""
        for run_id in [run_id for run_id in self._runs if run_id != keep and not run_is_active(run_id)]:
            del self._runs[run_id]
