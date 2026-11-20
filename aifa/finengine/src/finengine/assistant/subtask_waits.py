"""Учёт отчётов подзадач хода (CONTRACTS.md §3.1, §4).

Служба не знает, какой навык у подзадачи: она видит задачу ouroboros хода — по её первому вызову инструмента, по
кадру её итога. Ожидание task_id выполняет GigaAgent по своему хранилищу задач.
Служба учитывает кадр итога (шлюз хода, §2.3) или `aifa_report(status="done")`.

Состояние — в памяти процесса, как у `JobBoard`: служба пока одна реплика (`--workers 1`); кадр итога приходит туда,
где идёт ход, туда же приходят и вызовы инструментов.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from finengine.assistant.turn_gateway import SubtaskResult
from finengine.assistant.turn_record import run_is_active

# Отчётов `ok`/`error` на задачу ouroboros (ADR-0042 «Следствия»); `done` не считается: подзадача, исчерпавшая
# отчёты, всё равно должна уметь закончиться.
REPORTS_PER_TASK = 20


@dataclass(frozen=True)
class SubtaskOutcome:
    text: str
    failed: bool = False


@dataclass
class _Subtask:
    outcome: SubtaskOutcome | None = None
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

    def _sweep(self, *, keep: str) -> None:
        """Забыть подзадачи закончившихся ходов; без этого словарь рос бы до перезапуска."""
        for run_id in [run_id for run_id in self._runs if run_id != keep and not run_is_active(run_id)]:
            del self._runs[run_id]
