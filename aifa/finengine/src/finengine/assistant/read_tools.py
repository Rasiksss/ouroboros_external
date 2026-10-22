"""Читающие инструменты цикла `aifa_state` и `aifa_doc` (CONTRACTS.md §3.1, ADR-0040 п. 2, ADR-0043 п. 5).

Вне очереди: чтение не меняет `LoopSession` и отвечает сразу, пока идёт `aifa_move`. Доступны после любого
`aifa_open`; без открытого цикла `aifa_state` отдаёт только артефакты по `artifact_ids`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from finengine.assistant import doc_pages, ouro_view
from finengine.assistant.jobs import JobBoard
from finengine.assistant.goal_tools import OPEN_KIND
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_tools import no_loop
from finengine.assistant.ouro_view import STATE_BATCH, Refusal
from finengine.assistant.tool_call import BoundCall
from finengine.config import settings
from finengine.contracts import FileArtifact
from finengine.contracts.base import result_id_of
from finengine.ingestion.document_store import get_store
from finengine.skills.spec.dynamic_run import artifact_content, case_rows
from finengine.store.blobs import document_text
from finengine.store.cases import get_case_store

BEFORE_OPEN = "Сначала вызови aifa_open: чтение доступно после него."


class ArtifactIndexQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_type: str = Field(default="", max_length=100)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=20, ge=1, le=20, strict=True)


class StateTool:
    name = "aifa_state"
    root_only = False

    def __init__(self, board: JobBoard, desk: LoopDesk) -> None:
        self._board = board
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        ids = call.args.get("artifact_ids")
        view = call.args.get("view", "full")
        if not isinstance(view, str) or view not in {"full", "compact"}:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "view — full или compact.")
        index = None
        if "artifact_index" in call.args:
            try:
                index = ArtifactIndexQuery.model_validate(call.args["artifact_index"])
            except ValidationError:
                return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "artifact_index — output_type (строка), offset >= 0, limit от 1 до 20.")
            if ids:
                return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "artifact_ids и artifact_index запрашиваются отдельно.")
        if ids is not None and not _ids_ok(ids):
            return ouro_view.refusal(
                Refusal.BAD_ARGUMENTS, f"artifact_ids — список из не более {STATE_BATCH} непустых id."
            )
        loop = await self._desk.open_loop_of(call)
        limit = settings.ouroboros_result_limit
        if not ids and index is None:
            if loop is None:
                return no_loop()
            return await asyncio.to_thread(ouro_view.state, loop.session, limit=limit)
        if loop is None and self._board.of_kind(call.ctx.run_id, OPEN_KIND) is None:
            return no_loop(BEFORE_OPEN)
        rows = await asyncio.to_thread(case_rows, get_case_store(), call.record.user_id, call.record.case_id)
        if index is not None:
            return ouro_view.artifact_index(_index_entries(rows, index.output_type), offset=index.offset, count=index.limit, limit=limit)
        ids = ids or []
        found = _contents(rows, ids)
        missing = [item for item in ids if item not in dict(found)]
        if not found:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"В Case этого хода таких артефактов нет: {', '.join(missing)}.")
        return ouro_view.artifacts(found, missing, limit=limit, view=view)


def _artifact_id(payload: Any) -> str | None:
    return getattr(payload, "output_id", None) or result_id_of(payload)


def _index_entries(rows: list[dict], output_type: str) -> list[dict]:
    entries: dict[str, dict] = {}
    for row in rows:
        for payload in row.get("payloads") or ():
            kind = str(getattr(payload, "output_type", row["output_type"]))
            if output_type and kind != output_type:
                continue
            artifact_id = payload.artifact_id if isinstance(payload, FileArtifact) else _artifact_id(payload)
            if artifact_id:
                entries.setdefault(artifact_id, {"id": artifact_id, "output_type": kind, "concept": getattr(payload, "concept", None), "period_ref": getattr(payload, "period_ref", None)})
    return list(entries.values())


def _ids_ok(ids: Any) -> bool:
    return isinstance(ids, list) and len(ids) <= STATE_BATCH and all(isinstance(item, str) and item for item in ids)


def _contents(rows: list[dict], ids: list[str]) -> list[tuple[str, str]]:
    """Полное содержимое найденных артефактов в порядке запроса; id — evidence_id, у файла — artifact_id."""
    by_id: dict[str, str] = {}
    for row in rows:
        for payload in row.get("payloads") or ():
            artifact_id = payload.artifact_id if isinstance(payload, FileArtifact) else _artifact_id(payload)
            if artifact_id in ids:
                by_id.setdefault(artifact_id, artifact_content(payload))
    return [(item, by_id[item]) for item in ids if item in by_id]


class DocTool:
    name = "aifa_doc"
    root_only = False

    def __init__(self, board: JobBoard, desk: LoopDesk) -> None:
        self._board = board
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        loop = await self._desk.open_loop_of(call)
        if loop is None and self._board.of_kind(call.ctx.run_id, OPEN_KIND) is None:
            return no_loop(BEFORE_OPEN)
        doc_id, pages, query = (call.args.get(key) for key in ("doc_id", "pages", "query"))
        if not all(item is None or isinstance(item, str) for item in (doc_id, pages, query)):
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "doc_id, pages и query — строки.")
        if not doc_id:
            if pages or query:
                return ouro_view.refusal(Refusal.BAD_ARGUMENTS, "pages и query нужны вместе с doc_id.")
            return await asyncio.to_thread(_listing, call.record.doc_ids)
        if doc_id not in call.record.doc_ids:
            return ouro_view.refusal(Refusal.NO_DOCUMENT, "Такого документа в этом ходе нет: список — aifa_doc без аргументов.")
        text = await asyncio.to_thread(document_text, doc_id)
        if not text:
            return ouro_view.refusal(Refusal.NO_DOCUMENT, "Текст документа ещё не готов или не распознан: подожди и повтори.")
        return _reading(doc_id, text, pages, query)


def _listing(doc_ids: tuple[str, ...]) -> str:
    listed = []
    for doc_id in doc_ids:
        meta = get_store().get_document(doc_id)
        name = (getattr(meta, "company_name", "") or getattr(meta, "filename", "") or doc_id) if meta else doc_id
        listed.append((doc_id, str(name), len(doc_pages.pages_of(document_text(doc_id) or ""))))
    return ouro_view.documents(listed)


def _reading(doc_id: str, text: str, pages: str | None, query: str | None) -> str:
    all_pages = doc_pages.pages_of(text)
    if query:
        return ouro_view.doc_matches(doc_id, query, doc_pages.find(query, all_pages))
    shown = all_pages if not pages else doc_pages.page_range(pages, all_pages)
    if shown is None:
        return ouro_view.refusal(Refusal.BAD_ARGUMENTS, 'pages — номер или диапазон страниц: "3" или "3-5".')
    if not shown:
        return ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"Таких страниц нет: в документе {len(all_pages)}.")
    return ouro_view.doc_pages(doc_id, shown, pages or f"1-{all_pages[-1].number}", limit=settings.ouroboros_result_limit)
