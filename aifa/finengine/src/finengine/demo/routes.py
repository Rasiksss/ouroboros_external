"""Demo endpoints. Mounted at /demo/* in main.py.

Workflow when the user clicks "Demo: Роснефть":
  1. POST /demo/load?slug=rosneft        → backend
  2. Backend copies deploy/demo/rosneft/document.pdf into the user's docs dir
     with a fresh doc_id and owner_id = current_user.id, then indexes it
     normally (parse_pdf + add_document) so chat / search work.
  3. Backend returns the doc_meta + cached extraction (metrics + meta) from
     extraction.json — the UI synthesises an ExtractionMessage from this and
     adds it to the project, so the metrics table is filled instantly.
"""
from __future__ import annotations
import shutil
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from finengine.auth.db import User
from finengine.auth.dependencies import get_current_user
from finengine.config import settings
from finengine.demo import storage as demo_store
from finengine.ingestion.pdf_parser import parse_pdf
from finengine.models.document import DocumentMeta, DocType, CompanyType


router = APIRouter(prefix="/demo", tags=["demo"])


class DemoListItem(BaseModel):
    slug: str
    title: str
    description: str
    company_name: str


class DemoLoadResponse(BaseModel):
    doc_id: str
    filename: str
    pages: int
    company_name: str
    period_current: str
    period_prev: str
    unit: str
    metrics: list[dict]


@router.get("/list", response_model=list[DemoListItem])
def list_demos(_: User = Depends(get_current_user)):
    return [
        DemoListItem(
            slug=d.slug,
            title=d.title,
            description=d.description,
            company_name=d.company_name,
        )
        for d in demo_store.list_demos()
    ]


@router.post("/load", response_model=DemoLoadResponse)
async def load_demo(
    slug: str,
    user: User = Depends(get_current_user),
):
    # Ленивый импорт — разрыв цикла: routes ← main, а замок загрузки живёт в main.
    from finengine.api.main import _upload_lock
    from finengine.ingestion.document_store import get_store
    import asyncio

    meta = demo_store.get_demo(slug)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Demo '{slug}' not found")

    src_pdf = demo_store.get_pdf_path(slug)
    if not src_pdf.exists():
        raise HTTPException(status_code=404, detail="Demo PDF missing on disk")

    extraction = demo_store.load_extraction(slug)
    metrics = extraction.get("metrics", [])
    ext_meta = extraction.get("meta", {})

    # Copy PDF into the user's docs dir with a fresh doc_id.
    doc_id = str(uuid.uuid4())
    dst_filename = f"{meta.company_name or slug}.pdf"
    dst_path = settings.docs_dir / f"{doc_id}_{dst_filename}"
    shutil.copy(src_pdf, dst_path)

    doc_meta = DocumentMeta(
        doc_id=doc_id,
        filename=dst_filename,
        doc_type=DocType.IFRS_REPORT,
        company_name=meta.company_name,
        company_type=CompanyType.UNKNOWN,
        period=ext_meta.get("period_current") or meta.period_current,
        file_path=str(dst_path),
        owner_id=user.id,
    )

    # Parse + index inside the upload lock (same contract as /documents/upload).
    loop = asyncio.get_running_loop()

    def _parse_and_index():
        chunks = parse_pdf(dst_path, doc_meta, settings.chunk_size, settings.chunk_overlap)
        if not chunks:
            raise HTTPException(status_code=500, detail="Demo PDF failed to parse")
        get_store().add_document(doc_meta, chunks)
        return chunks

    try:
        async with _upload_lock:
            await loop.run_in_executor(None, _parse_and_index)
    except HTTPException:
        dst_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        dst_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))

    return DemoLoadResponse(
        doc_id=doc_id,
        filename=dst_filename,
        pages=doc_meta.total_pages,
        company_name=meta.company_name,
        period_current=ext_meta.get("period_current") or meta.period_current,
        period_prev=ext_meta.get("period_prev") or meta.period_prev,
        unit=ext_meta.get("unit") or meta.unit,
        metrics=metrics,
    )
