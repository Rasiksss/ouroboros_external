"""Фоновые задания хода и длинный опрос (CONTRACTS.md §3.0–§3.1, C3.4).

Задание — asyncio-задача в памяти этого процесса, как сам прогон в `run_orchestrator._runs`:
перезапуск службы задания теряет, а `aifa_wait` по потерянному отвечает `задание_не_найдено`.
Ход закончился или отменён — задание снимается само (`_supervise`), утечек задач нет.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from finengine.assistant.turn_record import run_is_active

log = logging.getLogger(__name__)


class RunEnded(Exception):  # noqa: N818 — не сбой, а исход: ход закончен, ждать нечего
    """Ход закончился или отменён, пока задание шло или его ждали."""


@dataclass(frozen=True)
class Job:
    job_id: str
    run_id: str
    kind: str
    task: asyncio.Task[str]


class JobBoard:
    def __init__(self, watch_sec: float) -> None:
        self._watch_sec = watch_sec
        self._jobs: dict[str, Job] = {}

    def start(self, run_id: str, kind: str, work: Coroutine[Any, Any, str], *, job_id: str | None = None) -> Job:
        self._sweep()
        job_id = job_id or uuid.uuid4().hex[:8]
        task = asyncio.create_task(self._supervise(run_id, work), name=f"ouro-job-{job_id}")
        task.add_done_callback(_log_failure)
        job = Job(job_id, run_id, kind, task)
        self._jobs[job_id] = job
        return job

    def find(self, run_id: str, job_id: str) -> Job | None:
        """Задание этого хода; чужого хода или неизвестное — `None` (`ОТКАЗ задание_не_найдено`)."""
        job = self._jobs.get(job_id)
        return job if job is not None and job.run_id == run_id else None

    def of_kind(self, run_id: str, kind: str) -> Job | None:
        """Задание хода этого вида, которое не сорвалось: по нему повторный вызов даёт тот же итог (C3.3)."""
        for job in self._jobs.values():
            if job.run_id == run_id and job.kind == kind and not _failed(job.task):
                return job
        return None

    async def wait(self, job: Job, seconds: float) -> str | None:
        """Готовый ответ задания или `None`, если за `seconds` не закончилось. Закончился ход — `RunEnded`."""
        await asyncio.wait({job.task}, timeout=seconds)
        if not job.task.done():
            return None
        if job.task.cancelled():
            raise RunEnded(job.run_id)
        return job.task.result()

    async def _supervise(self, run_id: str, work: Coroutine[Any, Any, str]) -> str:
        inner = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({inner}, timeout=self._watch_sec)
                if done:
                    return inner.result()
                if not run_is_active(run_id):
                    raise RunEnded(run_id)
        finally:
            if not inner.done():
                inner.cancel()
                await asyncio.gather(inner, return_exceptions=True)

    def _sweep(self) -> None:
        """Забыть закончившиеся задания закончившихся ходов; без этого словарь рос бы до перезапуска."""
        for job_id, job in list(self._jobs.items()):
            if job.task.done() and not run_is_active(job.run_id):
                del self._jobs[job_id]


def _failed(task: asyncio.Task[str]) -> bool:
    return task.done() and (task.cancelled() or task.exception() is not None)


def _log_failure(task: asyncio.Task[str]) -> None:
    """Системная ошибка задания — в журнал службы один раз, с трассой; её же забирает вызвавший."""
    if task.cancelled():
        return
    failure = task.exception()
    if failure is not None and not isinstance(failure, RunEnded):
        log.error("задание хода не выполнено: %s", task.get_name(), exc_info=failure)
