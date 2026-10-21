"""Открытие цикла хода для `aifa_open` (CONTRACTS.md §3.1, ADR-0060 п. 1)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from finengine.assistant.loop_desk import LoopDesk, LoopEmitter
from finengine.assistant.turn_record import TurnRecord
from finengine.roles.access import EXTRACTION_ROLES
from finengine.skills.production_runtime import open_agent_loop
from finengine.skills.spec.dynamic_run import LoopSession

# Документ хода распознаётся фоном с отправки сообщения (`defer_parse`), скан — секунды на страницу.
# Срок — как у прежнего чата (`llm/tools._fe_doc_text_wait`); дольше держит задание `aifa_open` (`ЖДИ`).
TEXT_WAIT_SEC = 240.0
TEXT_POLL_SEC = 1.0


@dataclass(frozen=True)
class NoDocument:
    """Открыть цикл не по чему: документа нет, он не выбран или его текст не готов; `why` — что сказать."""

    why: str


class LoopOpening:
    def __init__(self, desk: LoopDesk) -> None:
        self._desk = desk

    async def __call__(self, record: TurnRecord) -> LoopSession | NoDocument:
        doc_id = gate_document(record)
        if not doc_id:
            return NoDocument(
                "У хода нет отчёта: попроси пользователя приложить его." if not record.doc_ids
                else "В ходе несколько отчётов, а активный не выбран: попроси пользователя выбрать один."
            )
        text = await _document_text(doc_id)
        if not text:
            return NoDocument(
                "Текст отчёта ещё не готов или не распознан: попроси пользователя подождать или загрузить его заново."
            )
        opener = self._desk.opener(record.run_id, record.user_id, LoopEmitter.here(record.run_id))
        await asyncio.to_thread(
            open_agent_loop, open_loop=opener, owner_id=record.user_id, case_id=record.case_id, document_id=doc_id,
            document_text=text, message=record.message, role=interface_role(record.role),
            event_sink=None, trace_id=record.trace_id,
        )
        opened = self._desk.live(record.run_id)
        if opened is None:
            raise RuntimeError("цикл открыт, но не передан LoopDesk")
        return opened.session


def gate_document(record: TurnRecord) -> str:
    """Документ цикла: единственный документ хода или активный из них."""
    if len(record.doc_ids) == 1:
        return record.doc_ids[0]
    return record.active_doc_id if record.active_doc_id in record.doc_ids else ""


def interface_role(ui_role: str) -> str | None:
    """Роль, выбранная в интерфейсе, — точным кодом; «все показатели» и неизвестное — не роль-сужение."""
    code = (ui_role or "").strip().lower()
    return code if code in EXTRACTION_ROLES else None


async def _document_text(doc_id: str) -> str:
    from finengine.store.blobs import document_text_when_ready  # noqa: PLC0415 — тяжёлый слой хранилища

    return await document_text_when_ready(doc_id, timeout_s=TEXT_WAIT_SEC, poll_s=TEXT_POLL_SEC) or ""
