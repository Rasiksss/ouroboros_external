"""Открытые циклы ходов: сессии в памяти процесса, сохранение после каждого хода, подъём после перезапуска.

Цикл открывает `aifa_open` (CONTRACTS.md §3.1, ADR-0060), дальше его ведут вызовы
инструментов. Накопленное лежит в хранилище состояния службы (`state.LOOP_SESSION`, ADR-0023): ход цикла
может прийти на процесс, который сессию не открывал.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from finengine.assistant.tool_call import BoundCall
from finengine.assistant.turn_record import run_is_active
from finengine.skills import run_orchestrator
from finengine.skills.production_runtime import LoopOpener, restore_loop_session
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.skills.spec.loop_state import LoopOrigin, state_of
from finengine.store import blobs, state

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopEmitter:
    """События сессии — в журнал хода; вызывается из рабочего потока, пишет из потока цикла событий."""

    run_id: str
    loop: asyncio.AbstractEventLoop

    @classmethod
    def here(cls, run_id: str) -> LoopEmitter:
        return cls(run_id, asyncio.get_running_loop())

    def __call__(self, event: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(run_orchestrator.emit_to_run, self.run_id, event)


@dataclass(frozen=True)
class RunClosed:
    """Ход закончен или остановлен: сессия новых дорогих шагов не начинает (`LoopSession.is_cancelled`)."""

    run_id: str

    def __call__(self) -> bool:
        return not run_is_active(self.run_id)


@dataclass
class OpenLoop:
    run_id: str
    session: LoopSession
    origin: LoopOrigin
    user_id: int | str
    queue: asyncio.Lock = field(default_factory=asyncio.Lock)  # изменяющие вызовы хода — по одному (ADR-0043 п. 4)


@dataclass(frozen=True)
class HandOver:
    """Передача открытого цикла реестру: её вызывает стандартный путь из рабочего потока."""

    desk: LoopDesk
    run_id: str
    user_id: int | str
    emitter: LoopEmitter

    def __call__(self, session: LoopSession, origin: LoopOrigin) -> None:
        self.desk.adopt(self.run_id, self.user_id, self.emitter, session, origin)


class LoopDesk:
    def __init__(self) -> None:
        self._open: dict[str, OpenLoop] = {}
        self._restoring = asyncio.Lock()

    def opener(self, run_id: str, user_id: int | str, emitter: LoopEmitter) -> LoopOpener:
        return HandOver(self, run_id, user_id, emitter)

    def adopt(self, run_id: str, user_id: int | str, emitter: LoopEmitter, session: LoopSession, origin: LoopOrigin) -> None:
        self._sweep()
        session.event_sink.target = emitter  # type: ignore[attr-defined]  # SwitchSink from the loop opener
        session.is_cancelled = RunClosed(run_id)
        self._open[run_id] = OpenLoop(run_id, session, origin, user_id)
        session.market_checkpoint = lambda: self.save(run_id)
        self.save(run_id)

    def live(self, run_id: str) -> OpenLoop | None:
        return self._open.get(run_id)

    async def open_loop_of(self, call: BoundCall) -> OpenLoop | None:
        """Открытый цикл хода: из памяти, иначе из сохранённого; `None` — цикл в этом ходе не открывали."""
        run_id = call.ctx.run_id
        found = self._open.get(run_id)
        if found is not None:
            return found
        async with self._restoring:
            found = self._open.get(run_id)
            if found is None:
                found = await self._restore(run_id, call.record.user_id)
            return found

    def save(self, run_id: str) -> None:
        loop = self._open[run_id]
        state.put(
            state.LOOP_SESSION, owner=loop.user_id, key=run_id,
            doc={"origin": loop.origin.to_doc(), "state": state_of(loop.session)},
        )

    async def _restore(self, run_id: str, user_id: int | str) -> OpenLoop | None:
        doc = await asyncio.to_thread(state.get, state.LOOP_SESSION, owner=user_id, key=run_id)
        if doc is None:
            return None
        origin = LoopOrigin.from_doc(doc["origin"])
        text = ""
        if origin.document_id is not None:
            loaded_text = await asyncio.to_thread(blobs.document_text, origin.document_id)
            text = loaded_text or ""
        session = await asyncio.to_thread(
            restore_loop_session, origin, doc["state"], document_text=text,
            event_sink=LoopEmitter.here(run_id), is_cancelled=RunClosed(run_id),
        )
        log.info("цикл хода %s поднят из сохранённого: ходов %d", run_id[:8], session.turns)
        self._open[run_id] = restored = OpenLoop(run_id, session, origin, user_id)
        session.market_checkpoint = lambda: self.save(run_id)
        return restored

    def _sweep(self) -> None:
        """Забыть циклы закончившихся ходов — и в памяти, и в хранилище; без этого они копились бы."""
        for run_id, loop in list(self._open.items()):
            if not run_is_active(run_id):
                del self._open[run_id]
                state.delete_one(state.LOOP_SESSION, owner=loop.user_id, key=run_id)
