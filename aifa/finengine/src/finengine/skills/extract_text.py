"""Text-based entry to the finengine extraction engine (no document store).

For callers that hold raw OCR text instead of an indexed `doc_id` — e.g. the
Workstation station-skill, where the gateway has already run IDP-OCR and dropped
the text into the sandbox. Reuses the selected two-stage engine
unchanged by feeding it the text via a temp `.txt` sidecar + a minimal stub index
(the engine only needs `doc.{company_name,filename,period}` + an optional chunk
lookup for page-citation, which we leave empty). Accuracy, period selection and
role scoping stay identical to the live pipeline — no second implementation, no
drift.
"""
from __future__ import annotations

import uuid
from typing import AsyncGenerator, Optional

from finengine.config import settings
class _StubDoc:
    """Minimal stand-in for the indexed document — only the attributes the
    engine reads off `doc` (company_name / filename / period)."""

    def __init__(self, company_name: str = "", filename: str = "", period: str = "") -> None:
        self.company_name = company_name
        self.filename = filename
        self.period = period


class _StubIndex:
    """Satisfies the two index calls the full-context engine makes when it has
    no real document store: `get_document` (metadata) and `get_chunks_by_type`
    (page rebuild — empty here, so the engine falls back to flat text)."""

    def __init__(self, doc_id: str, doc: _StubDoc) -> None:
        self._doc_id = doc_id
        self._doc = doc

    def get_document(self, doc_id: str):
        return self._doc if doc_id == self._doc_id else None

    def get_chunks_by_type(self, kind: str, doc_id: Optional[str] = None):  # noqa: ARG002
        return []


async def extract_stream_from_text(
    text: str,
    *,
    company_type: str = "",
    role: str = "",
    model_id: Optional[str] = None,
    metric_enums: Optional[set[str]] = None,
    company_name: str = "",
    include_segments: bool = False,
    include_consolidated_group: bool = False,
    registry_override: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """Run the real finengine extraction on raw `text` (no index). Yields the
    canonical metric/status event stream.

    `company_type` ("non_financial" | "bank") and `metric_enums` (the cell's
    registry) should come from the classifier + `registry_for_profile`. Empty
    `company_type` lets the engine detect it from the text.

    `registry_override` — ВЕСЬ словарь `registry_for_profile(profile_id)`
    (extractable + calculated). Без него движок фильтрует по enum'ам ячейки, но
    строки показателей и формулы берёт из сгенерированного `data/metrics.json`
    — то есть правка ячейки до промпта не доезжает (#147). Передавать надо
    именно словарь, а не только enum'ы: формулы живут в `calculated`.
    """
    # Текст приезжает в станцию строкой, а движок просит его по `doc_id` —
    # значит публикуем под временным идентификатором ровно тем же способом,
    # что и распознанный текст загруженного файла. Своя запись файла означала
    # бы, что на S3 движок временный текст не найдёт.
    from finengine.store.blobs import save_document_text  # noqa: PLC0415

    tmp_id = f"sttext-{uuid.uuid4().hex}"
    save_document_text(tmp_id, text)
    stub = _StubIndex(tmp_id, _StubDoc(company_name=company_name, filename=company_name or tmp_id))
    # Единственный station extraction engine. Откат версии выполняется
    # выкладкой предыдущего артефакта; runtime-переключатель на параллельный
    # старый pipeline удалён в Срезе 14.
    from finengine.skills.two_stage_extractor import (
        extract_stream_finengine_dev_two_stage as _engine,
    )
    extra = {
        "extraction_batch_jobs": settings.extract_batch_jobs,
        "extraction_batch_size": settings.extract_batch_size,
        "extraction_batch_attempts": settings.extract_batch_attempts,
        "extraction_batch_strategy": settings.extract_batch_strategy,
        "extraction_ask_without_evidence": settings.extract_ask_without_evidence,
        "segment_extraction_batch_size": settings.extract_segment_batch_size,
    }
    try:
        async for ev in _engine(
            tmp_id,
            company_type or "unknown",
            index=stub,
            model_id=model_id,
            metric_enums=metric_enums,
            role=role,
            include_segments=include_segments,
            include_consolidated_group=include_consolidated_group,
            registry_override=registry_override,
            **extra,
        ):
            yield ev
    finally:
        from finengine.store.blobs import delete_document_text  # noqa: PLC0415

        delete_document_text(tmp_id)
