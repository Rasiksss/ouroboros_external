"""FastAPI application for Finengine."""
from __future__ import annotations
import asyncio
import json
import re
import secrets
import shutil
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import AsyncGenerator

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from finengine.auth.bootstrap import bootstrap_admin
from finengine.auth.db import User
from finengine.auth.dependencies import get_current_user, get_current_user_detached
from finengine.auth.billing import enforce_spend_budget
from finengine.auth.routes import router as auth_router
from finengine.demo.routes import router as demo_router
from finengine.config import settings
from finengine.email_intake import EmailIntake
from finengine.i18n import language_directive, set_reply_locale, t
from finengine.ingestion import archive
from finengine.models.document import DocumentMeta, DocType, CompanyType, ProcessingMode
from finengine.models.query import QueryRequest, QueryResponse
from finengine.orchestrator.agent import get_agent
from finengine.roles import (
    ALL_EXTRACTION_ROLE,
    build_formatted_table,
    build_role_plan,
    build_top_borrower_metric_rows,
    default_role_for_user,
    normalize_extraction_role,
    role_segment_policy,
    role_should_include_consolidated_group,
    role_should_include_segments,
    role_should_include_top_borrowers,
    user_can_extract_as,
    write_formatted_xlsx,
    write_generic_wide_xlsx,
)
from finengine.roles.dgr_adjustments import apply_dgr_calculated_adjustments
from finengine.skills.extractor import get_index as _get_index
from finengine.skills.metric_registry import (
    processing_mode_for_document,
    supports_metric_extraction,
)

# Sentry — must be initialized before the FastAPI app is created so the
# integration captures startup errors too. Empty DSN = SDK is a no-op.
if settings.sentry_dsn_backend:
    import sentry_sdk
    from sentry_sdk.integrations.starlette import StarletteIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration

    def _before_send(event, hint):
        # Strip the OpenRouter / Anthropic auth headers and proxy URL out of
        # exception payloads. The SDK already redacts cookies / Authorization,
        # but our HTTPS_PROXY / OPENROUTER_API_KEY may end up in repr() of
        # client errors.
        try:
            if "exception" in event:
                for exc in event["exception"].get("values", []):
                    val = exc.get("value", "")
                    if "openrouter.ai" in val or "anthropic.com" in val:
                        exc["value"] = "(redacted — contained provider URL)"
        except Exception:
            pass
        return event

    sentry_sdk.init(
        dsn=settings.sentry_dsn_backend,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        send_default_pii=False,
        before_send=_before_send,
    )

app = FastAPI(
    title="Finengine API",
    description="Financial IFRS Analysis Agent",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def resolve_reply_locale(request: Request, call_next):
    """Pin the answer language for the whole request from a single header.

    Resolved at the edge rather than per-endpoint on purpose: every surface
    that generates prose — chat, quick analysis, synthesis, admin analytics —
    needs it, and a new one should not have to remember to plumb a field
    through its own request model. The frontend sets the header once, in its
    SSE fetch wrapper.

    Starlette runs the endpoint in a task spawned after this line, and a task
    copies the context at creation, so everything downstream sees the value.
    """
    set_reply_locale(request.headers.get("X-Reply-Locale"))
    return await call_next(request)

# Singleton VectorIndex shared across API + chat agent + extractor skills.
# Until 2026-05-28 main.py had its own `VectorIndex()` here, and
# `skills/extractor.get_index()` had a separate one. Upload wrote to the
# main.py instance, chat read from the extractor.py instance — they never
# synced (each holds `_docs` in memory after `_load()`), so freshly
# uploaded documents looked invisible to the chat agent (no_meta).
# Fix: single source of truth via get_index().
index = _get_index()

# Serialize uploads — VectorIndex.add_document() is not thread-safe (FAISS
# write + pickle dump). Concurrent uploads would race; under the lock they
# queue. Chat / extract reads are unaffected because they hold no lock.
_upload_lock = asyncio.Lock()


app.include_router(auth_router)
app.include_router(demo_router)


def _migrate_legacy_docs():
    """Backfill owner_id=0 for documents indexed before the field existed.

    Pydantic doesn't apply field defaults to objects loaded from pickle —
    they're attribute-less, which would 500 on every list_documents call.
    Walk the dict, set the attribute via __dict__ directly, persist if any
    were missing.
    """
    if not hasattr(index, "_docs"):
        return
    changed = False
    for doc in index._docs.values():
        if not hasattr(doc, "owner_id"):
            try:
                object.__setattr__(doc, "owner_id", 0)
            except Exception:
                # pydantic.BaseModel: write into __dict__
                doc.__dict__["owner_id"] = 0
            changed = True
    if changed:
        index._save()


def _read_pdf_cover_text(file_path: Path, max_pages: int = 3) -> str:
    """Best-effort text read from the first PDF pages.

    IDP sidecars sometimes start after the SEC cover page, while the company
    name is only present there. This fallback is metadata-only and does not
    replace OCR text used by extraction.
    """
    if not file_path.exists():
        return ""
    try:
        import fitz  # type: ignore

        parts: list[str] = []
        with fitz.open(file_path) as pdf:
            for i in range(min(max_pages, len(pdf))):
                parts.append(pdf[i].get_text("text") or "")
        return "\n".join(parts)
    except Exception:
        return ""


def _read_pdf_page_count(file_path: Path) -> int:
    """Return PDF page count without running OCR or text extraction."""
    if not file_path.exists() or file_path.suffix.lower() != ".pdf":
        return 0
    try:
        import fitz  # type: ignore

        with fitz.open(file_path) as pdf:
            return int(len(pdf))
    except Exception:
        return 0


def _document_total_pages(doc: DocumentMeta) -> int:
    try:
        total_pages = int(getattr(doc, "total_pages", 0) or 0)
    except Exception:
        total_pages = 0
    if total_pages > 0:
        return total_pages
    file_path = getattr(doc, "file_path", "") or ""
    if not file_path:
        return 0
    return _read_pdf_page_count(Path(file_path))


def _looks_like_sec_cover_text(filename: str, text: str) -> bool:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", stem):
        return True
    upper = text.upper()
    return (
        "SECURITIES AND EXCHANGE COMMISSION" in upper
        or "FORM 10-K" in upper
        or "FORM 20-F" in upper
    )


def _detect_document_company_name(
    doc_id: str,
    filename: str,
    full_text: str | None,
    existing_name: str | None,
    *,
    file_path: Path | None = None,
) -> str:
    """Detect a clean company name from sidecar text, then PDF cover fallback."""
    from finengine.skills.bulk_extractor import (
        _detect_company_from_text,
        _prefer_detected_company_name,
    )

    detected = _detect_company_from_text(full_text or "")
    if not detected and filename.lower().endswith(".pdf"):
        pdf_path = file_path or (settings.docs_dir / f"{doc_id}_{filename}")
        cover_text = _read_pdf_cover_text(pdf_path)
        if _looks_like_sec_cover_text(filename, cover_text):
            detected = _detect_company_from_text(cover_text)
    return _prefer_detected_company_name(existing_name, detected, filename)


@app.on_event("startup")
async def warmup():
    """Seed admin user + migrate legacy docs.

    The embedding model is no longer pre-loaded here. After the full-context
    migration, uploads no longer embed PDFs (text sidecar instead of FAISS).
    The embedder still lazy-loads on demand for legacy chat fallback only.
    Pre-loading made startup hang for ~5 minutes when HuggingFace was
    slow / the proxy flaky.
    """
    bootstrap_admin()
    _migrate_legacy_docs()

    # Mark any runs that died with the previous container as session-lost.
    # Their journal files survived the restart but the in-memory
    # `_RunState` is gone — without this, the frontend would spin forever
    # waiting for events that will never come. Now covers every run kind
    # (chain / extract / quick / chat / synth / insights) via the generalised
    # orchestrator.
    try:
        from finengine.skills.run_orchestrator import recover_orphan_runs
        counts = recover_orphan_runs()
        if counts:
            import logging
            total = sum(counts.values())
            logging.getLogger(__name__).info(
                "Marked %d orphan run(s) as session-lost on startup: %s",
                total, counts,
            )
    except Exception:
        pass

    # Start the email-intake poller (no-ops unless IMAP creds are configured).
    _email_intake.start()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "documents": index.total_documents,
        "chunks": index.total_chunks,
    }


@app.get("/_sentry-test")
def _sentry_test():
    """Trigger a test exception so we can verify Sentry is wired up."""
    raise RuntimeError("sentry-test: backend integration check")


def _parse_doc_in_background(
    doc_id: str,
    file_path: Path,
    filename: str,
    ocr_engine: str = "idp",
    owner_id: int | None = None,
) -> None:
    """Parse an uploaded document into its stored text sidecar.

    PDFs always go through IDP-OCR. The ``ocr_engine`` argument is retained
    only for compatibility with already queued background tasks.
    """
    import logging
    log = logging.getLogger(__name__)

    try:
        import asyncio as _aio

        meta = index.get_document(doc_id)
        if not meta:
            return

        full_text: str | None = None
        actual_engine: str | None = None
        chunks_to_index: list = []

        if filename.lower().endswith(".pdf"):
            try:
                from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr

                raw_path = settings.docs_dir / f"{doc_id}.idpocr.v2.json"
                ocr_result = _aio.run(
                    call_idp_ocr(
                        file_path,
                        save_raw_to=raw_path,
                        parallel=settings.idp_ocr_parallel,
                    )
                )
                full_text = ocr_result.full_text
            except Exception as e:
                log.exception("IDP-OCR failed for %s: %s", doc_id, e)
                return

            if not full_text or not full_text.strip():
                log.error("IDP-OCR returned empty text for %s", doc_id)
                return
            actual_engine = "idp"
        elif filename.lower().endswith((".txt", ".md")):
            full_text = file_path.read_text(encoding="utf-8")
            actual_engine = "text_file"
        else:
            return

        if not full_text or not full_text.strip():
            return

        meta.ocr_engine = actual_engine or ocr_engine
        try:
            from finengine.ingestion.document_classifier import (
                apply_classification_to_meta,
                classify_document,
            )
            from finengine.llm.tracer import clear_trace_context, set_trace_context

            trace_user_id = owner_id if owner_id is not None else meta.owner_id
            set_trace_context(kind="classifier", doc_id=doc_id, user_id=trace_user_id)
            try:
                classification = _aio.run(
                    classify_document(
                        filename,
                        full_text,
                        doc_type_hint=meta.doc_type,
                        company_type_hint=meta.company_type,
                    )
                )
            finally:
                clear_trace_context()
            apply_classification_to_meta(meta, classification)
        except Exception as exc:
            log.warning("Document classification failed for %s: %s", doc_id, exc)

        chosen_name = _detect_document_company_name(
            doc_id,
            filename,
            full_text,
            meta.company_name,
            file_path=file_path,
        )
        if chosen_name:
            meta.company_name = chosen_name

        # Persist classification before exposing the ready text sidecar. This
        # keeps extraction from racing a second classification of the same doc.
        try:
            index.add_document(meta, chunks_to_index)
        except Exception:
            pass

        text_path = settings.docs_dir / f"{doc_id}.txt"
        text_path.write_text(full_text, encoding="utf-8")
    except Exception as e:
        log.exception("Background parse failed for %s: %s", doc_id, e)


def _register_upload(
    background: BackgroundTasks,
    *,
    doc_id: str,
    filename: str,
    file_path: Path,
    company_name: str,
    period: str,
    doc_type: str,
    company_type: str,
    owner_id: int,
) -> dict:
    """Index an already-saved upload and queue its parse. Returns the payload.

    Shared by the single-file endpoint and the archive one, so a document
    unpacked from a .zip is indistinguishable from a directly uploaded file.
    """
    meta = DocumentMeta(
        doc_id=doc_id,
        filename=filename,
        doc_type=DocType(doc_type) if doc_type else DocType.OTHER,
        company_name=company_name,
        company_type=CompanyType(company_type) if company_type else CompanyType.UNKNOWN,
        period=period,
        total_pages=_read_pdf_page_count(file_path),
        file_path=str(file_path),
        owner_id=owner_id,
        # Product policy: every uploaded PDF goes through quality-first IDP-OCR.
        # Stale browser preferences still sending "auto"/"pymupdf" are ignored.
        ocr_engine="idp",
    )
    # Register before the background parse — the UI sees the doc immediately.
    index.add_document(meta, [])

    # Schedule the heavy work for after the response is sent.
    background.add_task(
        _parse_doc_in_background,
        doc_id,
        file_path,
        filename,
        meta.ocr_engine,
        owner_id,
    )

    return {
        "doc_id": doc_id,
        "filename": filename,
        "chunks": 0,
        "pages": meta.total_pages,
        "doc_type": meta.doc_type,
        "processing_mode": meta.processing_mode,
        "language": meta.language,
        "processing": True,
    }


@app.post("/documents/upload")
async def upload_document(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    company_name: str = Form(""),
    period: str = Form(""),
    doc_type: str = Form(""),
    company_type: str = Form("unknown"),
    ocr_engine: str = Form("idp"),
    user: User = Depends(get_current_user),
):
    """Upload a document (PDF/TXT/MD).

    The endpoint returns as soon as the file is on disk — the actual PDF
    parse + .txt sidecar generation runs as a background task. The UI can
    show the document and PDF viewer immediately. /extract/stream waits for
    the .txt to appear and otherwise emits a "still processing" error.
    """
    doc_id = str(uuid.uuid4())
    filename = file.filename or "document.pdf"
    file_path = settings.docs_dir / f"{doc_id}_{filename}"

    # Save file synchronously — small enough that this is fast.
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return _register_upload(
        background,
        doc_id=doc_id,
        filename=filename,
        file_path=file_path,
        company_name=company_name,
        period=period,
        doc_type=doc_type,
        company_type=company_type,
        owner_id=user.id,
    )


@app.post("/documents/upload/archive")
async def upload_archive(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    company_name: str = Form(""),
    period: str = Form(""),
    doc_type: str = Form(""),
    company_type: str = Form("unknown"),
    user: User = Depends(get_current_user),
):
    """Upload a .zip and register each supported member as its own document.

    Members the pipeline cannot parse are dropped, not rejected — an archive
    that also holds spreadsheets, images or a README still uploads fine, and
    the response names what was left out so the UI can say so.

    Extraction runs to completion before anything is indexed: a bomb caught
    halfway through would otherwise leave the user with a half-registered
    archive and no way to tell which documents are missing.
    """
    saved: list[tuple[str, str, Path]] = []  # (doc_id, filename, path)
    try:
        with archive.open_archive(file.file) as zf:
            members, skipped = archive.list_supported(zf)
            if not members:
                raise archive.ArchiveError(
                    "archive_no_supported_files",
                    "В архиве нет ни одного файла PDF, TXT или MD.",
                )
            budget = archive.MAX_TOTAL_BYTES
            for info in members:
                doc_id = str(uuid.uuid4())
                name = archive.member_display_name(info.filename)
                path = settings.docs_dir / f"{doc_id}_{name}"
                budget -= await run_in_threadpool(
                    archive.copy_member, zf, info, path, budget=budget
                )
                saved.append((doc_id, name, path))
    except archive.ArchiveError as exc:
        for _, _, path in saved:
            path.unlink(missing_ok=True)
        status = 413 if exc.code in {"archive_too_large", "archive_too_many_files"} else 400
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": exc.message},
        )

    documents = [
        _register_upload(
            background,
            doc_id=doc_id,
            filename=name,
            file_path=path,
            company_name=company_name,
            period=period,
            doc_type=doc_type,
            company_type=company_type,
            owner_id=user.id,
        )
        for doc_id, name, path in saved
    ]
    return {
        "documents": documents,
        # Names are for a "what was left out" hint, so a handful is enough;
        # the count stays exact.
        "skipped": skipped[:20],
        "skipped_count": len(skipped),
    }


# ── Email file intake ───────────────────────────────────────────────────────
async def _ingest_email_pdf(user_id: int, filename: str, data: bytes) -> str:
    """Register an emailed PDF as user `user_id`'s document and schedule OCR.

    The bytes-over-email counterpart of POST /documents/upload: same metadata,
    page count and background IDP-OCR, minus the HTTP request context.
    """
    doc_id = str(uuid.uuid4())
    safe_name = filename or "document.pdf"
    file_path = settings.docs_dir / f"{doc_id}_{safe_name}"
    file_path.write_bytes(data)
    total_pages = _read_pdf_page_count(file_path)
    meta = DocumentMeta(
        doc_id=doc_id,
        filename=safe_name,
        doc_type=DocType.OTHER,
        company_type=CompanyType.UNKNOWN,
        total_pages=total_pages,
        file_path=str(file_path),
        owner_id=user_id,
        ocr_engine="idp",
        source="email",
    )
    index.add_document(meta, [])
    # Heavy IDP-OCR parse off the event loop (it calls asyncio.run internally,
    # so it must not run on the loop). Fire-and-forget — the doc appears in the
    # user's list immediately and flips to ready once the .txt sidecar lands.
    asyncio.get_running_loop().run_in_executor(
        None,
        _parse_doc_in_background,
        doc_id,
        file_path,
        safe_name,
        "idp",
        user_id,
    )
    return doc_id


_email_intake = EmailIntake(
    host=settings.imap_host,
    port=settings.imap_port,
    user=settings.imap_user,
    password=settings.imap_password,
    address=settings.email_intake_address,
    ingest=_ingest_email_pdf,
)


class _EmailIntakeArm(BaseModel):
    email: str


def _intake_window_payload(w) -> dict:
    return {
        "armed": True,
        "configured": _email_intake.configured,
        "address": _email_intake.address,
        "address_ascii": _email_intake.address_ascii,
        "from_email": w.from_email,
        "expires_at": w.expires_at,
        "received": [
            {"doc_id": r.doc_id, "filename": r.filename, "ts": r.ts} for r in w.received
        ],
    }


@app.post("/email-intake/arm")
async def email_intake_arm(payload: _EmailIntakeArm, user: User = Depends(get_current_user)):
    """Open a 30-minute window: pick up PDFs emailed from `payload.email`."""
    if not _email_intake.configured:
        raise HTTPException(status_code=503, detail={"code": "email_intake_not_configured", "message": "Приём файлов с почты не настроен"})
    try:
        w = _email_intake.arm(user.id, payload.email)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_email", "message": "Некорректный email"})
    return _intake_window_payload(w)


@app.get("/email-intake/status")
async def email_intake_status(user: User = Depends(get_current_user)):
    w = _email_intake.get(user.id)
    if w is None or not w.active():
        return {
            "armed": False,
            "configured": _email_intake.configured,
            "address": _email_intake.address,
            "address_ascii": _email_intake.address_ascii,
        }
    return _intake_window_payload(w)


@app.post("/email-intake/disarm")
async def email_intake_disarm(user: User = Depends(get_current_user)):
    _email_intake.disarm(user.id)
    return {"armed": False}


@app.get("/admin/documents/{doc_id}/idpocr.json")
def download_idp_ocr_raw(doc_id: str, user: User = Depends(get_current_user)):
    """Admin-only download of the raw IDP-OCR response for a document.

    Saved by aifa_baseline on every successful OCR call; use it to inspect
    what the OCR actually returned (markdown, tables, page count) when an
    AIFA baseline run lands few values.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    # Try v2 (new per-page PNG pipeline) first, fall back to v1 (old whole-PDF
    # mode) for runs captured before the OCR client switch.
    for suffix in (".idpocr.v2.json", ".idpocr.json"):
        p = settings.docs_dir / f"{doc_id}{suffix}"
        if p.exists():
            return FileResponse(p, media_type="application/json", filename=p.name)
    raise HTTPException(
        status_code=404,
        detail="No IDP-OCR response saved. Run extract in AIFA baseline mode first.",
    )


@app.get("/admin/extract-runs.jsonl")
def download_extract_runs(user: User = Depends(get_current_user)):
    """Admin-only download of every extraction run captured so far.

    Each line in the response is one run record (see run_logger.log_run for
    the shape). Used by the benchmark builder + ad-hoc analysis: filter by
    `arch` (finengine / aifa_dev / aifa_baseline) and `company_type`,
    aggregate found / total against GT.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from finengine.skills.run_logger import runs_path
    p = runs_path()
    if not p.exists():
        return Response(content="", media_type="application/x-ndjson")
    return FileResponse(p, media_type="application/x-ndjson", filename="extract_runs.jsonl")


@app.get("/admin/chat-runs.jsonl")
def download_chat_runs(user: User = Depends(get_current_user)):
    """Admin-only download of the chat-turn audit log. Each line is one
    user→agent turn (prompt + full response + session/user/doc context).
    Used to debug what real users are asking and what the agent answered."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from finengine.skills.run_logger import chat_runs_path
    p = chat_runs_path()
    if not p.exists():
        return Response(content="", media_type="application/x-ndjson")
    return FileResponse(p, media_type="application/x-ndjson", filename="chat_runs.jsonl")


@app.get("/admin/runs")
def list_extract_runs(
    user: User = Depends(get_current_user),
    limit: int = 200,
):
    """Admin-only: parsed extract_runs.jsonl as JSON, with broken-flag and
    OCR-availability hints, for the /admin/runs UI page.

    Each row carries enough info to drive the UI: timestamp, doc_id,
    arch, model, found/total, elapsed (in minutes), broken flag with
    reason, an `idpocr_available` flag (so the UI can disable the link
    when no cache exists), and the full diag for the expand-row view.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from finengine.skills.run_logger import runs_path
    p = runs_path()
    out: list[dict] = []
    if not p.exists():
        return {"runs": out, "total": 0}

    with open(p, encoding="utf-8") as f:
        lines = f.readlines()

    # newest-first, then trim to limit
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Broken-run classification — same rules as
        # tests/build_unified_benchmark.py:classify_broken
        broken = False
        reason = "OK"
        if r.get("error"):
            broken, reason = True, f"error: {str(r['error'])[:80]}"
        else:
            found = int(r.get("found") or 0)
            total = int(r.get("total") or 0)
            metrics = r.get("metrics") or []
            if total == 0:
                broken, reason = True, "total=0"
            elif found == 0:
                broken, reason = True, "found=0"
            elif len(metrics) < 0.3 * total:
                broken, reason = True, f"truncated: {len(metrics)}/{total} metrics"
            else:
                samples = (r.get("diag") or {}).get("samples") or []
                if samples:
                    n_err = sum(1 for s in samples if s.get("reason") == "llm_error" or s.get("error"))
                    if n_err > 0.5 * len(samples):
                        broken, reason = True, f"{n_err}/{len(samples)} sample LLM errors"

        # Check both v2 and v1 cache paths for the OCR raw response.
        doc_id = r.get("doc_id") or ""
        idpocr_available = False
        for suffix in (".idpocr.v2.json", ".idpocr.json"):
            if (settings.docs_dir / f"{doc_id}{suffix}").exists():
                idpocr_available = True
                break

        elapsed_s = float(r.get("elapsed_s") or 0)
        out.append({
            "ts": r.get("ts"),
            "git_ver": r.get("git_ver") or "",
            "git_sha": r.get("git_sha") or "",
            "doc_id": doc_id,
            "filename": r.get("filename"),
            "arch": r.get("arch"),
            "model_id": r.get("model_id"),
            "company_type": r.get("company_type"),
            "company_name": r.get("company_name"),
            "found": r.get("found"),
            "total": r.get("total"),
            "elapsed_min": round(elapsed_s / 60.0, 2),
            "broken": broken,
            "broken_reason": reason,
            "idpocr_available": idpocr_available,
            "diag": r.get("diag") or {},
            "error": r.get("error"),
        })
        if len(out) >= limit:
            break

    return {"runs": out, "total": len(lines)}


@app.get("/admin/llm_traces/{run_id}")
def get_llm_traces(
    run_id: str,
    user: User = Depends(get_current_user),
    date: str | None = None,
):
    """Admin-only: every LLM call recorded for one run, with full prompts +
    responses + tokens + latency + errors. Backs the diagnostic view used
    when extract / chains / consensus runs misbehave (e.g. cost overrun,
    consensus_failed cascade) — without raw I/O we can't tell whether the
    model misread the prompt, hallucinated, errored on auth, etc.

    `date` (YYYY-MM-DD, optional) narrows search to one trace file. If
    omitted, scans the last 14 days of trace files in newest-first order.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    import datetime as _dt
    trace_dir = settings.docs_dir / "llm_traces"
    if not trace_dir.exists():
        return {"run_id": run_id, "traces": [], "total": 0}

    if date:
        candidates = [trace_dir / f"{date}.jsonl"]
    else:
        # Newest first, last 14 days max
        today = _dt.date.today()
        candidates = []
        for i in range(14):
            d = today - _dt.timedelta(days=i)
            p = trace_dir / f"{d.isoformat()}.jsonl"
            if p.exists():
                candidates.append(p)

    traces: list[dict] = []
    for p in candidates:
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("run_id") == run_id:
                        traces.append(rec)
        except Exception:
            continue

    # Within a run, newest-last is the natural read order — matches the
    # order calls actually fired. The journal is already chronological.
    return {"run_id": run_id, "traces": traces, "total": len(traces)}


class MetricsExportRow(BaseModel):
    enum: str
    name_ru: str = ""
    name_en: str = ""
    segment: str = ""
    dimension: str = ""
    value_current: object | None = None
    value_prev: object | None = None
    period_values: list[dict] = Field(default_factory=list)
    unit: str = ""
    page: object | None = None
    source_form: str = ""
    source_text: str = ""
    is_calculated: bool = False
    formula: str = ""


class MetricsExportRequest(BaseModel):
    company: str = ""
    company_name: str = ""
    company_type: str = ""
    role: str = ""
    language: str = ""
    period_current: str = ""
    period_prev: str = ""
    unit: str = ""
    formatted: dict | None = None
    rows: list[MetricsExportRow]


class MetricsRoleFormatRequest(BaseModel):
    company_name: str = ""
    company_type: str = ""
    role: str = ""
    language: str = ""
    period_current: str = ""
    period_prev: str = ""
    period_calendar: str = ""
    unit: str = ""
    rows: list[MetricsExportRow]


def _role_periods_from_metrics(metrics: list[dict]) -> list[str]:
    periods: list[str] = []
    for metric in metrics:
        for pv in metric.get("period_values") or []:
            if isinstance(pv, dict) and pv.get("period"):
                periods.append(str(pv.get("period")))
    return periods


def _apply_dgr_metric_cache_adjustments(
    *,
    plan,
    metrics: list[dict],
    period_current: str = "",
    period_prev: str = "",
) -> list[dict]:
    if plan.role != "dgr":
        return metrics
    periods = [
        p
        for p in [period_current, period_prev, *_role_periods_from_metrics(metrics)]
        if p
    ]
    return [
        apply_dgr_calculated_adjustments(
            metric,
            metrics,
            periods,
            period_current=period_current,
            period_prev=period_prev,
        )
        for metric in metrics
    ]


@app.get("/admin/registry/export.xlsx")
def admin_registry_export(user: User = Depends(get_current_user)):
    """Admin-only: consolidated snapshot of all per-cell registries
    (standard × language × industry × roles → metrics, formulas, hints,
    aliases) as one downloadable xlsx — what's on the server at a glance."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    from urllib.parse import quote

    from finengine.roles.registry_export import build_registry_export_workbook

    buf = build_registry_export_workbook()
    fname = "Сводный_реестр_метрик.xlsx"
    disposition = (
        'attachment; filename="registry_consolidated.xlsx"; '
        f"filename*=UTF-8''{quote(fname)}"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


def _resolve_upload_role(user: User, role: str | None) -> str:
    """Role the registry template/upload acts for. Explicit role must be
    granted to the user; `all` is a picker pseudo-role, not a taggable one."""
    requested = normalize_extraction_role(role, default=default_role_for_user(user))
    if role and not user_can_extract_as(user, requested):
        raise HTTPException(status_code=403, detail="Role is not available for this user")
    if requested == ALL_EXTRACTION_ROLE:
        raise HTTPException(status_code=400, detail="Pick a concrete role for registry upload")
    return requested


@app.get("/registry/upload-template.xlsx")
def registry_upload_template(role: str | None = None, user: User = Depends(get_current_user)):
    """Template for user-uploaded metrics: empty fill-in sheet + instruction +
    the role's current registry as reference."""
    from urllib.parse import quote

    from finengine.skills.registry_upload import build_template_workbook

    requested = _resolve_upload_role(user, role)
    buf = build_template_workbook(requested)
    fname = f"Шаблон_показателей_{requested}.xlsx"
    disposition = (
        f'attachment; filename="registry_template_{requested}.xlsx"; '
        f"filename*=UTF-8''{quote(fname)}"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@app.post("/registry/upload")
async def registry_upload(
    file: UploadFile = File(...),
    role: str | None = Form(default=None),
    user: User = Depends(get_current_user),
):
    """Upload a filled metric template. Valid rows land in the per-cell
    overlays tagged with the uploader's role (the matrix and the role's plan
    pick them up at runtime, no redeploy); broken rows come back in the report."""
    import logging

    from finengine.skills.registry_upload import apply_upload, parse_upload_workbook

    requested = _resolve_upload_role(user, role)
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(status_code=413, detail={"code": "registry_file_too_large", "message": "Файл больше 5 МБ"})
    rows, file_errors = parse_upload_workbook(content)
    if file_errors:
        raise HTTPException(status_code=400, detail="; ".join(file_errors))
    if not rows:
        raise HTTPException(status_code=400, detail={"code": "registry_no_metric_rows", "message": "В файле нет ни одной строки с показателем"})
    report = apply_upload(requested, rows)
    logging.getLogger(__name__).info(
        "registry upload by %s as %s: +%s/~%s, rejected %s, cells %s",
        user.username, requested, report["added"], report["updated"],
        len(report["rejected"]), ",".join(report["cells"]),
    )
    return report


@app.post("/metrics/format-role")
def metrics_format_role(req: MetricsRoleFormatRequest, user: User = Depends(get_current_user)):
    """Re-render an already extracted metric cache into a role-specific layout."""
    requested_role = normalize_extraction_role(req.role, default=default_role_for_user(user))
    if req.role and not user_can_extract_as(user, requested_role):
        raise HTTPException(status_code=403, detail="Role is not available for this user")

    plan = build_role_plan(requested_role, req.company_type)
    metrics = [r.model_dump() for r in req.rows]
    metrics = _apply_dgr_metric_cache_adjustments(
        plan=plan,
        metrics=metrics,
        period_current=req.period_current,
        period_prev=req.period_prev,
    )
    meta = {
        "period_current": req.period_current,
        "period_prev": req.period_prev,
        "period_calendar": req.period_calendar,
        "language": req.language,
        "unit": req.unit,
        "company_type": req.company_type,
        "company_name": req.company_name,
    }
    table = None
    if settings.role_formatters_enabled and plan.config:
        table = build_formatted_table(plan=plan, metrics=metrics, meta=meta)

    return {
        "role": plan.role,
        "role_label": plan.role_label,
        "company_type": plan.company_type,
        "role_format": plan.format,
        "output_enums": sorted(plan.output_enums) if plan.output_enums is not None else None,
        "formatted": table,
    }


@app.post("/metrics/export.xlsx")
def metrics_export_xlsx(req: MetricsExportRequest, user: User = Depends(get_current_user)):
    """Render the user's extracted-metrics table to a downloadable .xlsx.

    The CSV path lives entirely in the frontend (BOM + utf-8). Real Excel
    needs a proper xlsx, so we shape it here with openpyxl — same dep we
    already use for the bench file.
    """
    requested_role = normalize_extraction_role(req.role, default=default_role_for_user(user))
    if req.role and not user_can_extract_as(user, requested_role):
        raise HTTPException(status_code=403, detail="Role is not available for this user")

    if settings.role_formatters_enabled and req.role:
        plan = build_role_plan(requested_role, req.company_type)
        if plan.config:
            metrics = _apply_dgr_metric_cache_adjustments(
                plan=plan,
                metrics=[r.model_dump() for r in req.rows],
                period_current=req.period_current,
                period_prev=req.period_prev,
            )
            table = req.formatted or build_formatted_table(
                plan=plan,
                metrics=metrics,
                meta={
                    "period_current": req.period_current,
                    "period_prev": req.period_prev,
                    "language": req.language,
                    "unit": req.unit,
                    "company_type": req.company_type,
                    "company_name": req.company_name or req.company,
                },
            ) or req.formatted
            if table:
                buf = write_formatted_xlsx(table)
                from urllib.parse import quote

                safe_name = (req.company or "metrics").strip() or "metrics"
                fname = f"{safe_name}_{plan.role}_{req.period_current or ''}.xlsx".replace(" ", "_")
                ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", fname) or "metrics.xlsx"
                disposition = (
                    f'attachment; filename="{ascii_fname}"; '
                    f"filename*=UTF-8''{quote(fname)}"
                )
                return StreamingResponse(
                    buf,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": disposition},
                )
        elif requested_role in {"other", "all"}:
            buf = write_generic_wide_xlsx(
                [r.model_dump() for r in req.rows],
                {
                    "period_current": req.period_current,
                    "period_prev": req.period_prev,
                    "unit": req.unit,
                    "company_type": req.company_type,
                    "language": req.language,
                },
            )
            from urllib.parse import quote

            safe_name = (req.company or "metrics").strip() or "metrics"
            fname = f"{safe_name}_{requested_role}_{req.period_current or ''}.xlsx".replace(" ", "_")
            ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", fname) or "metrics.xlsx"
            disposition = (
                f'attachment; filename="{ascii_fname}"; '
                f"filename*=UTF-8''{quote(fname)}"
            )
            return StreamingResponse(
                buf,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": disposition},
            )

    import io
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    if ws is None:
        ws = wb.create_sheet()
    ws.title = "Wide"
    ws_long = wb.create_sheet("Long")

    cur_label = req.period_current or "Текущий"
    prev_label = req.period_prev or "Предыдущий"

    def _has_period_value(pv: dict) -> bool:
        return pv.get("value") is not None

    def _period_value_rows(r: MetricsExportRow) -> list[dict]:
        return [
            pv
            for pv in (r.period_values or [])
            if isinstance(pv, dict)
            and str(pv.get("period") or "").strip()
            and _has_period_value(pv)
        ]

    dynamic_periods: list[str] = []
    seen_periods: set[str] = set()
    for r in req.rows:
        for pv in _period_value_rows(r):
            p = str(pv.get("period") or "").strip()
            if p and p not in seen_periods:
                seen_periods.add(p)
                dynamic_periods.append(p)

    if dynamic_periods:
        headers = ["Метрика", "Enum", "Сегмент", "Разрез", *dynamic_periods, "Единица", "Источник", "Calc", "Формула"]
    else:
        headers = ["Метрика", "Enum", "Сегмент", "Разрез", cur_label, prev_label, "Δ %", "Единица", "Источник", "Calc", "Формула"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    long_headers = [
        "Метрика",
        "Enum",
        "Сегмент",
        "Разрез",
        "Период",
        "Значение",
        "Единица",
        "Страница",
        "Форма",
        "Source text",
        "Calc",
        "Формула",
    ]
    ws_long.append(long_headers)
    for c in ws_long[1]:
        c.font = Font(bold=True)

    def _as_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _delta(curr, prev) -> float | None:
        curr_f = _as_float(curr)
        prev_f = _as_float(prev)
        if curr_f is None or prev_f is None or prev_f == 0:
            return None
        return (curr_f - prev_f) / abs(prev_f) * 100

    include_source_form = requested_role not in {"other", "all"} or not req.language or req.language.strip().lower() == "ru"

    def _source(r: MetricsExportRow) -> str:
        refs: dict[int, str] = {}

        def add(page_raw, form_raw="") -> None:
            try:
                page = int(page_raw or 0)
            except Exception:
                page = 0
            if page <= 0:
                return
            form = str(form_raw or "").strip()
            if page not in refs or (form and not refs[page]):
                refs[page] = form

        add(r.page, r.source_form)
        for pv in _period_value_rows(r):
            add(pv.get("page"), pv.get("source_form") or r.source_form)
        return ", ".join(
            f"стр. {page} ({refs[page]})" if include_source_form and refs[page] else f"стр. {page}"
            for page in sorted(refs)
        )

    def _append_long_row(
        r: MetricsExportRow,
        period: str,
        value,
        unit: str,
        page,
        source_form: str,
        source_text: str,
    ) -> None:
        ws_long.append([
            r.name_ru or r.enum,
            r.enum,
            r.segment or "",
            r.dimension or "",
            period,
            value,
            unit,
            page or "",
            source_form or "",
            source_text or "",
            "Y" if r.is_calculated else "",
            r.formula or "",
        ])

    for r in req.rows:
        if dynamic_periods:
            vals = {str(pv.get("period") or ""): pv.get("value") for pv in _period_value_rows(r)}
            ws.append([
                r.name_ru or r.enum,
                r.enum,
                r.segment or "",
                r.dimension or "",
                *[vals.get(p) for p in dynamic_periods],
                r.unit or req.unit or "",
                _source(r),
                "Y" if r.is_calculated else "",
                r.formula or "",
            ])
        else:
            d = _delta(r.value_current, r.value_prev)
            ws.append([
                r.name_ru or r.enum,
                r.enum,
                r.segment or "",
                r.dimension or "",
                r.value_current,
                r.value_prev,
                None if d is None else round(d, 2),
                r.unit or req.unit or "",
                _source(r),
                "Y" if r.is_calculated else "",
                r.formula or "",
            ])
        period_rows = _period_value_rows(r)
        if period_rows:
            for pv in period_rows:
                _append_long_row(
                    r,
                    str(pv.get("period") or ""),
                    pv.get("value"),
                    str(pv.get("unit") or r.unit or req.unit or ""),
                    pv.get("page"),
                    str(pv.get("source_form") or r.source_form or ""),
                    str(pv.get("source_text") or ""),
                )
        else:
            if r.value_current is not None:
                _append_long_row(
                    r,
                    cur_label,
                    r.value_current,
                    r.unit or req.unit or "",
                    r.page,
                    r.source_form,
                    r.source_text,
                )
            if r.value_prev is not None:
                _append_long_row(
                    r,
                    prev_label,
                    r.value_prev,
                    r.unit or req.unit or "",
                    r.page,
                    r.source_form,
                    r.source_text,
                )

    # Column widths
    widths = (
        [42, 32, 24, 18] + [14] * len(dynamic_periods) + [12, 18, 6, 38]
        if dynamic_periods
        else [42, 32, 24, 18, 14, 14, 10, 12, 18, 6, 38]
    )
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    long_widths = [42, 32, 24, 18, 16, 14, 12, 10, 14, 50, 6, 38]
    for i, w in enumerate(long_widths, start=1):
        ws_long.column_dimensions[get_column_letter(i)].width = w
    ws_long.freeze_panes = "A2"
    # Right-align numeric columns
    numeric_cols = range(5, 5 + len(dynamic_periods)) if dynamic_periods else (5, 6, 7)
    for col in numeric_cols:
        for cell in ws[get_column_letter(col)][1:]:
            cell.alignment = Alignment(horizontal="right")
    for cell in ws_long["F"][1:]:
        cell.alignment = Alignment(horizontal="right")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe_name = (req.company or "metrics").strip() or "metrics"
    fname = f"{safe_name}_{req.period_current or ''}.xlsx".replace(" ", "_")
    # Cyrillic filenames break a raw `filename="..."` header (Safari refuses
    # the response with "Load failed", Chrome strips characters silently).
    # RFC 6266 / 5987 says non-ASCII names must use `filename*=UTF-8''<pct>`,
    # with an ASCII fallback for older clients. quote() percent-encodes the
    # name; we also keep an ASCII-only `filename=` so dumb clients still get
    # something.
    from urllib.parse import quote
    ascii_fname = re.sub(r"[^\x20-\x7e]+", "_", fname) or "metrics.xlsx"
    disposition = (
        f'attachment; filename="{ascii_fname}"; '
        f"filename*=UTF-8''{quote(fname)}"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@app.get("/documents/{doc_id}/status")
def document_status(doc_id: str, user: User = Depends(get_current_user)):
    """Returns whether the .txt sidecar is ready (PDF parse finished)."""
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")
    text_path = settings.docs_dir / f"{doc_id}.txt"
    return {
        "doc_id": doc_id,
        "processing": not text_path.exists(),
        "company_name": doc.company_name or "",
        "pages": _document_total_pages(doc),
        "doc_type": getattr(doc, "doc_type", DocType.OTHER),
        "processing_mode": processing_mode_for_document(doc),
        "company_type": getattr(doc, "company_type", CompanyType.UNKNOWN),
        "profile_id": getattr(doc, "profile_id", "") or "",
        "standard": getattr(doc, "reporting_standard", "") or "",
        "classification_low_axes": getattr(doc, "classification_low_axes", []) or [],
        "classification_fallback_used": bool(
            getattr(doc, "classification_fallback_used", False)
        ),
    }


_PREMIUM_MODEL_KEY_TO_ID = {
    "kimi": "moonshotai/kimi-k2.5",
    "glm": "z-ai/glm-5.1",
}
_PREMIUM_MODEL_IDS = set(_PREMIUM_MODEL_KEY_TO_ID.values())
_BASE_MODEL_ID = "qwen/qwen3.6-plus-04-02"


def _enforce_model_grant(model_id: str | None, user: User) -> str | None:
    """Return the model id to use, falling back to the base model if the user
    asked for a premium one without a matching grant. Admins keep full access.
    """
    if not model_id or model_id == _BASE_MODEL_ID:
        return model_id
    if user.is_admin:
        return model_id
    granted_keys = {k.strip() for k in (user.allowed_models or "").split(",") if k.strip()}
    granted_ids = {_PREMIUM_MODEL_KEY_TO_ID[k] for k in granted_keys if k in _PREMIUM_MODEL_KEY_TO_ID}
    if model_id in _PREMIUM_MODEL_IDS and model_id not in granted_ids:
        return _BASE_MODEL_ID
    return model_id


def _user_can_see(doc: DocumentMeta, user: User) -> bool:
    """Owner or admin (admins also see legacy owner_id=0 docs).

    getattr fallback handles documents that were indexed before the
    owner_id field existed — pydantic won't synthesise the default for
    unpickled objects, so direct attribute access throws AttributeError.
    """
    if user.is_admin:
        return True
    return getattr(doc, "owner_id", 0) == user.id


@app.get("/documents")
def list_documents(user: User = Depends(get_current_user)):
    """List documents owned by the caller (admins see everything)."""
    docs = index.list_documents()
    visible = [d for d in docs if _user_can_see(d, user)]
    return [
        {
            "doc_id": d.doc_id,
            "filename": d.filename,
            "doc_type": d.doc_type,
            "processing_mode": processing_mode_for_document(d),
            "processing": not (settings.docs_dir / f"{d.doc_id}.txt").exists(),
            "company_name": d.company_name,
            "period": d.period,
            "language": d.language,
            "company_type": getattr(d, "company_type", CompanyType.UNKNOWN),
            "pages": _document_total_pages(d),
            # Empty for legacy docs uploaded before this field existed.
            "ocr_engine": getattr(d, "ocr_engine", "") or "",
            # "email" for files picked up from the intake mailbox, else "".
            "source": getattr(d, "source", "") or "",
            "profile_id": getattr(d, "profile_id", "") or "",
            "standard": getattr(d, "reporting_standard", "") or "",
            "classification_low_axes": getattr(d, "classification_low_axes", []) or [],
            "classification_fallback_used": bool(
                getattr(d, "classification_fallback_used", False)
            ),
        }
        for d in visible
    ]


@app.get("/documents/{doc_id}/file")
def get_document_file(doc_id: str, user: User = Depends(get_current_user)):
    """Serve the original PDF file."""
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found in index")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found in index")
    if not doc.file_path:
        raise HTTPException(status_code=404, detail="File path not recorded (document indexed without file)")
    path = Path(doc.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"PDF file not found on disk: {path.name}")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=doc.filename,
        content_disposition_type="inline",
    )


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user: User = Depends(get_current_user)):
    """Remove a document from the index."""
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete file
    if doc.file_path:
        Path(doc.file_path).unlink(missing_ok=True)

    index.remove_document(doc_id)
    return {"status": "deleted", "doc_id": doc_id}


async def _resolve_processing_mode(doc: DocumentMeta) -> ProcessingMode:
    """Resolve routing only after the OCR sidecar has been completed."""
    from finengine.ingestion.document_classifier import (
        apply_classification_to_meta,
        classification_from_meta,
        classify_document,
    )

    cached_classification = classification_from_meta(doc)
    classification = cached_classification
    if classification is None or classification.fallback_used:
        text_path = settings.docs_dir / f"{doc.doc_id}.txt"
        if not text_path.exists():
            return processing_mode_for_document(doc)
        try:
            refreshed_classification = await classify_document(
                doc.filename,
                text_path.read_text(encoding="utf-8"),
                doc_type_hint=doc.doc_type,
                company_type_hint=doc.company_type,
            )
        except Exception:
            return processing_mode_for_document(doc)
        if cached_classification is None or not refreshed_classification.fallback_used:
            classification = refreshed_classification
            apply_classification_to_meta(doc, classification)
            index.update_document_meta(doc)
    return classification.processing_mode


@app.post("/documents/{doc_id}/extract")
async def extract_metrics_endpoint(
    doc_id: str,
    company_type: str = "unknown",
    user: User = Depends(get_current_user),
):
    """Extract all financial metrics from an indexed document (non-streaming)."""
    enforce_spend_budget(user)
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")
    processing_mode = await _resolve_processing_mode(doc)
    if not supports_metric_extraction(processing_mode):
        raise HTTPException(
            status_code=409,
            detail=(
                "Классификатор не обнаружил в документе полный или сокращенный "
                "набор отчетных показателей. Для этого документа используйте «Инсайты»."
            ),
        )
    from finengine.skills.bulk_extractor import _extract_async
    result = await _extract_async(doc_id, company_type, index=index)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


async def _sse_with_heartbeat(
    events: AsyncGenerator[dict, None], *, heartbeat_sec: float = 15.0
) -> AsyncGenerator[bytes, None]:
    """Format run events as SSE frames, injecting a comment heartbeat during
    quiet gaps so a long extraction is not dropped by browsers/proxies that
    time out idle connections. The backend keeps working regardless — this only
    keeps the client attached so it still receives the final result.

    A ``: ping`` line is an SSE comment: the frontend parser ignores it (no
    ``data:`` prefix), but the bytes keep the HTTP stream alive. We never cancel
    the in-flight ``__anext__`` on timeout — that would corrupt the generator;
    we just re-wait on the same pending event.
    """
    it = events.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_sec)
            if not done:
                yield b": ping\n\n"
                continue
            try:
                ev = pending.result()
            except StopAsyncIteration:
                return
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
            pending = asyncio.ensure_future(it.__anext__())
    finally:
        pending.cancel()


@app.post("/documents/{doc_id}/extract/stream")
async def extract_metrics_stream(
    doc_id: str,
    company_type: str = "unknown",
    limit: int | None = None,
    parallel: int = 8,
    model_id: str | None = None,
    arch: str = "finengine",
    # Sub-mode for Finengine DEV: "registry" reuses the same 101-metric scope
    # as Finengine Baseline / AIFA Baseline; "unrestricted" runs the free-form
    # path. Ignored for other archs — they always use their default flow.
    mode: str = "registry",
    include_segments: bool | None = None,
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    role: str = "",
    # Reasoning effort for reasoning-capable models (GLM-5.x, o1/o3).
    # "low" / "medium" (default) / "high". Ignored for non-reasoning models.
    reasoning_effort: str | None = None,
    user: User = Depends(get_current_user_detached),
):
    from finengine.llm.openrouter import set_reasoning_effort
    set_reasoning_effort(reasoning_effort)
    """Extract financial metrics with per-metric SSE streaming (highest accuracy).

    Query params:
      limit — extract only the first N metrics (skips calculated). For quick UI tests.
      parallel — concurrent OpenRouter calls (default 8 — 100 metrics in ~5 min).
      model_id — override OpenRouter model id (default: settings.openrouter_model).
      arch — extraction architecture: "finengine" (full-context, default) or "aifa"
             (per-metric reranker → top-3 tables → LLM; RU non-financial only).
    """
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")
    processing_mode = await _resolve_processing_mode(doc)
    if not supports_metric_extraction(processing_mode):
        raise HTTPException(
            status_code=409,
            detail=(
                "Классификатор не обнаружил в документе полный или сокращенный "
                "набор отчетных показателей. Для этого документа используйте «Инсайты»."
            ),
        )
    enforce_spend_budget(user)
    # Enforce per-user model gating. Anyone can use the base model (default
    # if model_id is None). Premium ids ("kimi", "glm" → moonshotai/kimi-k2.5,
    # z-ai/glm-5.1) require a matching grant on the user record.
    model_id = _enforce_model_grant(model_id, user)

    role_feature_active = (
        settings.role_formatters_enabled
        and arch == "finengine_dev"
        and mode != "unrestricted"
    )
    requested_role = normalize_extraction_role(role, default=default_role_for_user(user))
    if role_feature_active and not user_can_extract_as(user, requested_role):
        raise HTTPException(status_code=403, detail="Role is not available for this user")
    effective_role = requested_role if role_feature_active else role

    if arch == "aifa_baseline":
        from finengine.skills.aifa_baseline import extract_stream_aifa_baseline as _stream_fn
    elif arch in ("aifa", "aifa_dev"):
        from finengine.skills.aifa import extract_stream_aifa as _stream_fn
    elif arch == "finengine_rag":
        # Per-metric FAISS retrieval → LLM. Was previously labelled
        # "finengine_dev" — renamed when a real DEV sandbox was introduced.
        from finengine.skills.bulk_extractor import _extract_stream as _stream_fn
    elif arch == "finengine_dev":
        # Experimental sandbox: free-form extract — model returns whatever
        # financial figures it can find without registry constraints.
        # New (v100): user can pick "по реестру" in the UI — same 101-metric
        # registry path as Finengine Baseline. Default stays "registry" so
        # the dropdown's first item is the safer one.
        if mode == "unrestricted":
            from finengine.skills.bulk_extractor import _extract_stream_unrestricted as _stream_fn
        else:
            from finengine.skills.bulk_extractor import extract_stream_finengine_dev as _stream_fn
    elif arch == "finengine_consensus":
        # Triumvirat — three LLMs (Qwen + Kimi + DeepSeek V4 Pro) extract
        # each metric independently, agreement / debate / final null.
        # Optimised for precision over recall (consensus-failed → null).
        # Gated on the `consensus` per-user grant; the grant unlocks the
        # arch but does NOT grant standalone Kimi / DeepSeek access.
        if not user.is_admin:
            granted_keys = {k.strip() for k in (user.allowed_models or "").split(",") if k.strip()}
            if "consensus" not in granted_keys:
                raise HTTPException(
                    status_code=403,
                    detail="Triumvirat (consensus) mode requires the `consensus` grant.",
                )
        from finengine.skills.consensus_extractor import extract_stream_consensus as _stream_fn
    else:
        # arch in ("finengine", "finengine_baseline", "" / unset / unknown):
        # default to full-context extraction.
        from finengine.skills.bulk_extractor import _extract_stream_full_context as _stream_fn

    # Resumable extract: spawn the run as an asyncio.create_task that
    # outlives this request. Page reload / VPN drop cancels the SSE
    # streaming response, but the worker keeps going and the journal on
    # disk lets the frontend reattach via /extract/runs/{run_id}/stream.
    import time as _t
    from finengine.skills.run_logger import log_run
    from finengine.skills import run_orchestrator

    started_at = _t.time()
    # Capture state lives in the closure shared between the runner
    # (which mutates it) and `_on_done` (which reads it for the audit
    # log row). Plain dict — only one task touches it at a time.
    capture: dict = {
        "metrics": [],
        "all_metrics": [],
        "company_type": company_type,
        "company_name": doc.company_name or "",
        "period_current": "",
        "period_prev": "",
        "periods": [],
        "period_calendar": "",
        "error": None,
        "diag": {},
        # Phase A axes (matrix refactor). Populated by the runner once the
        # .txt sidecar is available. See _classifier.py for details.
        "profile_id": "",
        "standard": "",
        "language": "",
        "profile_confidence_low": False,
        "doc_type": "",
        "processing_mode": "",
        "classification_low_axes": [],
        "classification_fallback_used": False,
        "role": requested_role if role_feature_active else "",
        "role_label": "",
        "role_format": "",
    }

    # `parallel` only applies to per-metric extractors. Full-context /
    # unrestricted send the whole report in one LLM call — no concurrency
    # to dial. Pass the query-param value through only for those archs.
    stream_kwargs: dict = {
        "index": index,
        "limit": limit,
        "model_id": model_id,
    }
    if arch in ("aifa_baseline", "aifa", "aifa_dev", "finengine_rag", "finengine_consensus"):
        stream_kwargs["parallel"] = parallel
    if arch in ("finengine", "finengine_baseline", "finengine_dev", "") and mode != "unrestricted":
        if role_feature_active:
            stream_kwargs["include_segments"] = False
            stream_kwargs["include_consolidated_group"] = False
        else:
            stream_kwargs["include_segments"] = (
                bool(include_segments)
                or (
                    include_segments is None
                    and ("диэ" in effective_role.lower() or "die" in effective_role.lower())
                )
            )
            stream_kwargs["include_consolidated_group"] = False
        stream_kwargs["role"] = effective_role
        stream_kwargs["segment_mode"] = segment_mode
        stream_kwargs["segment_context_mode"] = segment_context_mode

    async def runner() -> AsyncGenerator[dict, None]:
        # The runner sees the raw events and tees them into `capture`.
        # Yields are what the orchestrator journals + broadcasts.
        # ── Phase A: classify the document before extract starts. ──────
        # We wait for the .txt sidecar (same one every _stream_fn waits on
        # downstream), classify, then yield a `profile_detected` event so
        # the frontend can show the badge immediately. Failures here are
        # NOT fatal — extract still runs, just without the new axes.
        classified_profile = None
        pdf_text_cached: str | None = None
        try:
            from pathlib import Path as _Path
            from finengine.config import settings as _settings
            from finengine.skills.bulk_extractor import _wait_for_pdf_text
            from finengine.ingestion.document_classifier import (
                apply_classification_to_meta,
                classification_from_meta,
                classify_document,
            )

            txt_path = _Path(_settings.docs_dir) / f"{doc_id}.txt"
            # If parsing already finished (most common case) this returns
            # immediately; otherwise we wait up to 15 min like the
            # downstream extractors.
            if await _wait_for_pdf_text(txt_path):
                pdf_text_cached = txt_path.read_text(encoding="utf-8")
                chosen_name = _detect_document_company_name(
                    doc_id,
                    doc.filename,
                    pdf_text_cached,
                    doc.company_name or capture["company_name"],
                )
                if chosen_name:
                    capture["company_name"] = chosen_name
                    if chosen_name != (doc.company_name or ""):
                        try:
                            doc.company_name = chosen_name
                            index.update_document_meta(doc)
                        except Exception:
                            pass
                classification = classification_from_meta(doc)
                if classification is None:
                    classification = await classify_document(
                        doc.filename,
                        pdf_text_cached,
                        doc_type_hint=doc.doc_type,
                        company_type_hint=doc.company_type,
                    )
                    apply_classification_to_meta(doc, classification)
                    index.update_document_meta(doc)
                profile = classification.profile
                classified_profile = profile
                capture["profile_id"] = profile.profile_id
                capture["standard"] = profile.standard
                capture["language"] = profile.language
                capture["profile_confidence_low"] = profile.confidence_low
                capture["doc_type"] = classification.doc_type.value
                capture["processing_mode"] = classification.processing_mode.value
                capture["classification_low_axes"] = list(classification.low_axes)
                capture["classification_fallback_used"] = classification.fallback_used
                capture["diag"]["classification"] = {
                    "doc_type": classification.doc_type.value,
                    "processing_mode": classification.processing_mode.value,
                    "low_axes": list(classification.low_axes),
                    "fallback_used": classification.fallback_used,
                }
                yield {
                    "type": "profile_detected",
                    "profile_id": profile.profile_id,
                    "standard": profile.standard,
                    "language": profile.language,
                    "company_type": profile.company_type,
                    "confidence_low": profile.confidence_low,
                    "reason": classification.reason,
                    "doc_type": classification.doc_type.value,
                    "processing_mode": classification.processing_mode.value,
                    "classification_confidence_low": classification.confidence_low,
                    "classification_low_axes": list(classification.low_axes),
                    "classification_fallback_used": classification.fallback_used,
                }
        except Exception as exc:
            # Classifier must never block extract. Log and continue.
            capture["diag"]["classifier_error"] = f"{type(exc).__name__}: {exc}"[:200]

        # Route the registry-mode Finengine path through its classified matrix
        # cell. AIFA, RAG, consensus and unrestricted modes keep their own
        # streams. The flag remains only as an emergency rollback switch.
        cell_stream = None
        from finengine.config import settings as _settings
        if (
            _settings.extractor_matrix_enabled
            and classified_profile is not None
            and arch in ("finengine", "finengine_baseline", "finengine_dev", "")
            and mode != "unrestricted"
        ):
            from finengine.skills.extractors import (
                ExtractContext,
                get_extractor,
            )
            cell_stream = get_extractor(classified_profile.profile_id)
            if cell_stream is not None:
                capture["diag"]["routed_to"] = f"matrix:{classified_profile.profile_id}"

        role_plan = None
        role_output_enums: set[str] | None = None
        role_extraction_enums: set[str] | None = None
        if role_feature_active:
            plan_company_type = (
                classified_profile.company_type
                if classified_profile is not None
                else company_type
            )
            role_plan = build_role_plan(
                requested_role,
                plan_company_type,
                profile_id=(
                    classified_profile.profile_id if classified_profile is not None else None
                ),
            )
            capture["role"] = role_plan.role
            capture["role_label"] = role_plan.role_label
            capture["role_format"] = role_plan.format
            capture["diag"]["role"] = role_plan.role
            capture["diag"]["role_format"] = role_plan.format
            segment_policy = role_segment_policy(role_plan.role, role_plan.company_type)
            segments_enabled = segment_policy.include_segments
            consolidated_group_enabled = role_should_include_consolidated_group(
                role_plan.role,
                role_plan.company_type,
            )
            stream_kwargs["include_segments"] = segments_enabled
            stream_kwargs["include_consolidated_group"] = consolidated_group_enabled
            top_borrowers_enabled = role_should_include_top_borrowers(
                role_plan.role,
                role_plan.company_type,
            )
            stream_kwargs["include_top_borrowers"] = top_borrowers_enabled
            if segments_enabled:
                stream_kwargs["segment_mode"] = segment_policy.segment_mode
                stream_kwargs["segment_context_mode"] = segment_policy.segment_context_mode
            capture["diag"]["include_segments"] = segments_enabled
            capture["diag"]["include_consolidated_group"] = consolidated_group_enabled
            capture["diag"]["include_top_borrowers"] = top_borrowers_enabled
            capture["diag"]["segment_mode"] = stream_kwargs.get("segment_mode", "extended")
            capture["diag"]["segment_context_mode"] = stream_kwargs.get("segment_context_mode", "full")
            if role_plan.format == "unsupported":
                doc_kind = "банковскими" if role_plan.company_type == "bank" else "нефинансовыми"
                capture["error"] = (
                    f"Role {role_plan.role} is not supported for {role_plan.company_type}"
                )
                yield {
                    "type": "error",
                    "message": (
                        f"Извините, роль «{role_plan.role_label}» пока не работает "
                        f"с {doc_kind} отчетами."
                    ),
                }
                return
            role_output_enums = role_plan.output_enums
            role_extraction_enums = role_plan.extraction_enums
            if role_extraction_enums is not None and not role_extraction_enums:
                capture["error"] = (
                    f"Role {role_plan.role} has no metric scope for {role_plan.company_type}"
                )
                yield {
                    "type": "error",
                    "message": (
                        f"Для роли «{role_plan.role_label}» пока не настроены показатели "
                        "для этого типа документа."
                    ),
                }
                return
        elif (
            arch in ("finengine", "finengine_baseline", "finengine_dev", "")
            and mode != "unrestricted"
            and classified_profile is not None
        ):
            # No role feature (e.g. Finengine Baseline): segments are decided by
            # the document's profile (cell), exactly like finengine_dev —
            # non-financial → on, banks → off. The legacy includeSegments toggle
            # is no longer consulted.
            seg = role_should_include_segments(effective_role, classified_profile.company_type)
            stream_kwargs["include_segments"] = seg
            capture["diag"]["include_segments"] = seg

        if cell_stream is not None:
            routed_company_type = classified_profile.company_type
            ctx = ExtractContext(
                doc_id=doc_id,
                pdf_text=pdf_text_cached or "",
                company_type=routed_company_type,
                model_id=model_id,
                metric_enums=sorted(role_extraction_enums) if role_extraction_enums else None,
                role=effective_role,
                include_segments=bool(stream_kwargs.get("include_segments", True)),
                include_consolidated_group=bool(stream_kwargs.get("include_consolidated_group", False)),
                include_top_borrowers=bool(stream_kwargs.get("include_top_borrowers", False)),
                segment_mode=stream_kwargs.get("segment_mode", "extended"),
                segment_context_mode=stream_kwargs.get("segment_context_mode", "full"),
                limit=limit,
                index=index,
                profile_reason=classified_profile.reason if classified_profile else "",
            )
            event_iter = cell_stream(ctx)
        else:
            capture["diag"]["routed_to"] = f"legacy:{arch or 'finengine'}"
            stream_kwargs_for_run = dict(stream_kwargs)
            if role_extraction_enums:
                stream_kwargs_for_run["metric_enums"] = role_extraction_enums
            routed_company_type = (
                classified_profile.company_type
                if classified_profile is not None
                else company_type
            )
            event_iter = _stream_fn(doc_id, routed_company_type, **stream_kwargs_for_run)

        from finengine.skills.metric_registry import METRIC_NAME_EN_BY_ENUM

        name_en_by_enum = dict(METRIC_NAME_EN_BY_ENUM)
        if classified_profile is not None:
            try:
                from finengine.skills.metric_schema import registry_for_profile

                profile_registry = registry_for_profile(classified_profile.profile_id)
                name_en_by_enum.update(profile_registry.get("name_en_by_enum") or {})
            except Exception:
                pass

        async for event in event_iter:
            etype = event.get("type")
            if etype in {"metric", "hidden_metric", "progress"} and not event.get("name_en"):
                enum = str(event.get("enum") or "").strip().upper()
                name_en = name_en_by_enum.get(enum, "")
                if name_en:
                    event = {**event, "name_en": name_en}
            if etype == "metric":
                if role_feature_active and role_plan is not None and role_plan.role == "dgr":
                    event = apply_dgr_calculated_adjustments(
                        event,
                        capture["all_metrics"],
                        capture.get("periods", []),
                        period_current=capture["period_current"],
                        period_prev=capture["period_prev"],
                    )
                capture["all_metrics"].append(event)
                if role_output_enums is not None:
                    enum = str(event.get("enum") or "").strip().upper()
                    if enum not in role_output_enums:
                        continue
                capture["metrics"].append(event)
            elif etype == "hidden_metric":
                hidden_metric = dict(event)
                hidden_metric["type"] = "metric"
                capture["all_metrics"].append(hidden_metric)
                if (
                    role_feature_active
                    and role_plan is not None
                    and role_plan.role == "other"
                    and role_plan.company_type == "bank"
                ):
                    try:
                        top_n = max(1, int(getattr(settings, "top_borrowers_count", 5) or 5))
                    except Exception:
                        top_n = 5
                    public_metrics = build_top_borrower_metric_rows(
                        hidden_metric,
                        meta={
                            "period_current": capture["period_current"],
                            "period_prev": capture["period_prev"],
                            "periods": capture.get("periods", []),
                            "period_calendar": capture.get("period_calendar", ""),
                            "unit": hidden_metric.get("unit") or "",
                            "company_type": capture["company_type"],
                            "company_name": capture["company_name"],
                        },
                        top_n=top_n,
                    )
                    for public_metric in public_metrics:
                        capture["all_metrics"].append(public_metric)
                        capture["metrics"].append(public_metric)
                        yield public_metric
                continue
            elif etype == "start":
                capture["company_type"] = event.get("company_type", capture["company_type"])
                if event.get("company_name") and not capture["company_name"]:
                    capture["company_name"] = event.get("company_name")
                capture["period_current"] = event.get("period_current", "")
                capture["period_prev"] = event.get("period_prev", "")
                capture["periods"] = event.get("periods", capture.get("periods", []))
                capture["period_calendar"] = event.get("period_calendar", capture.get("period_calendar", ""))
                if capture.get("language"):
                    event = dict(event)
                    event["language"] = capture.get("language")
                if "_diag_tables_total" in event:
                    capture["diag"]["tables_total"] = event["_diag_tables_total"]
                if role_output_enums is not None:
                    event = dict(event)
                    event["total_metrics"] = len(role_output_enums)
                    event["total_extractable"] = len(role_output_enums)
                    event["role"] = role_plan.role if role_plan else requested_role
                    event["role_label"] = role_plan.role_label if role_plan else ""
                    event["role_format"] = role_plan.format if role_plan else ""
                if capture["company_name"]:
                    event = dict(event)
                    event["company_name"] = capture["company_name"]
            elif etype == "progress" and role_output_enums is not None:
                enum = str(event.get("enum") or "").strip().upper()
                if enum and enum not in role_output_enums:
                    continue
            elif etype == "done":
                capture["company_type"] = event.get("company_type", capture["company_type"])
                if event.get("company_name") and not capture["company_name"]:
                    capture["company_name"] = event.get("company_name")
                capture["period_current"] = event.get("period_current", capture["period_current"])
                capture["period_prev"] = event.get("period_prev", capture["period_prev"])
                capture["periods"] = event.get("periods", capture.get("periods", []))
                capture["period_calendar"] = event.get("period_calendar", capture.get("period_calendar", ""))
                if capture["company_name"]:
                    event = dict(event)
                    event["company_name"] = capture["company_name"]
                if capture.get("language"):
                    event = dict(event)
                    event["language"] = capture.get("language")
                if "_diag" in event:
                    capture["diag"].update(event["_diag"])
                # Surface cost + token totals on the public `done` event.
                if "tokens_in" in capture["diag"]:
                    event["tokens_in"] = capture["diag"].get("tokens_in")
                    event["tokens_out"] = capture["diag"].get("tokens_out")
                    event["tokens_cached"] = capture["diag"].get("tokens_cached", 0)
                    event["cost_usd"] = capture["diag"].get("cost_usd", 0.0)
                if role_plan is not None:
                    event["role"] = role_plan.role
                    event["role_label"] = role_plan.role_label
                    event["role_format"] = role_plan.format
                    if role_plan.config:
                        try:
                            formatted = build_formatted_table(
                                plan=role_plan,
                                metrics=capture["all_metrics"],
                                meta={
                                    "period_current": capture["period_current"],
                                    "period_prev": capture["period_prev"],
                                    "periods": capture.get("periods", []),
                                    "period_calendar": capture.get("period_calendar", ""),
                                    "language": capture.get("language", ""),
                                    "unit": event.get("unit") or "",
                                    "company_type": capture["company_type"],
                                    "company_name": capture["company_name"],
                                },
                            )
                            if formatted:
                                event["formatted"] = formatted
                        except Exception as exc:
                            capture["diag"]["role_formatter_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )[:300]
            elif etype == "error":
                capture["error"] = event.get("message", "")
            # Strip internal diag fields before journaling — they only ever
            # belong in capture["diag"], not in the per-event SSE.
            client_event = {k: v for k, v in event.items() if not k.startswith("_diag")}
            yield client_event

    async def _on_done(state: run_orchestrator.RunState) -> None:
        try:
            log_run(
                doc_id=doc_id,
                filename=doc.filename,
                arch=arch,
                model_id=model_id,
                company_type=capture["company_type"],
                company_name=capture["company_name"],
                period_current=capture["period_current"],
                period_prev=capture["period_prev"],
                user_id=getattr(user, "id", None),
                started_at=started_at,
                metrics=capture["metrics"],
                error=capture["error"] or state.error,
                diag=capture["diag"],
                # Phase A — matrix axes; "" when classifier failed.
                profile_id=capture.get("profile_id", ""),
                standard=capture.get("standard", ""),
                language=capture.get("language", ""),
                profile_confidence_low=capture.get("profile_confidence_low", False),
                reasoning_effort=reasoning_effort or "",
                mode=mode,
            )
        except Exception:
            pass
        # Persist extracted company name back into doc metadata so future
        # listings show the clean name, not the raw filename.
        captured_name = capture.get("company_name") or ""
        if captured_name and captured_name != (doc.company_name or ""):
            try:
                doc.company_name = captured_name
                index.update_document_meta(doc)
            except Exception:
                pass

    run_id = run_orchestrator.start_run(
        runner,
        kind="extract",
        meta={
            "doc_id": doc_id,
            "filename": doc.filename,
            "arch": arch,
            "model_id": model_id or "",
            "user_id": getattr(user, "id", None),
        },
        on_done=_on_done,
    )

    async def event_gen() -> AsyncGenerator[bytes, None]:
        # Frontend persists `run_id` so a reload / VPN drop can reattach
        # via GET /extract/runs/{run_id}/stream.
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id}, ensure_ascii=False)}\n\n".encode()
        async for chunk in _sse_with_heartbeat(run_orchestrator.subscribe(run_id, kind="extract")):
            yield chunk

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/extract/runs/{run_id}/stream")
async def extract_run_resume(
    run_id: str,
    user: User = Depends(get_current_user_detached),
):
    """Re-subscribe to a previously-started extract run.

    The backend keeps the worker alive independently of the original
    request — page reload / VPN drop cancels the streaming response but
    not the task. This endpoint replays the on-disk journal then tails
    live events until the run finishes.

    Auth: only the user who started the run (per its meta) sees its
    events. Admins see everything.
    """
    from finengine.skills import run_orchestrator
    state = run_orchestrator.get_run(run_id)
    if state is not None:
        owner = state.meta.get("user_id")
        if not user.is_admin and owner is not None and owner != user.id:
            raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for chunk in _sse_with_heartbeat(run_orchestrator.subscribe(run_id, kind="extract")):
            yield chunk

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/documents/{doc_id}/insights/stream")
async def extract_insights_endpoint(
    doc_id: str,
    model_id: str | None = None,
    role: str = "",
    reasoning_effort: str | None = None,
    user: User = Depends(get_current_user_detached),
):
    """Extract evidence-backed insights from one non-report source."""
    doc = index.get_document(doc_id)
    if not doc or not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    enforce_spend_budget(user)
    model_id = _enforce_model_grant(model_id, user)
    requested_role = normalize_extraction_role(
        role,
        default=default_role_for_user(user),
    )

    from finengine.skills.insights import (
        InsightsNotReadyError,
        InsightsUnavailableError,
        SUPPORTED_INSIGHT_ROLES,
        extract_insights_stream,
        prepare_insights_context,
    )

    if requested_role not in SUPPORTED_INSIGHT_ROLES:
        raise HTTPException(
            status_code=400,
            detail="Insights are available only for the General and DIE roles.",
        )
    if not user_can_extract_as(user, requested_role):
        raise HTTPException(status_code=403, detail="Role is not available for this user")

    from finengine.llm.openrouter import set_reasoning_effort

    set_reasoning_effort(reasoning_effort)
    try:
        context = await prepare_insights_context(
            index=index,
            doc_id=doc_id,
            role=requested_role,
        )
    except (InsightsNotReadyError, InsightsUnavailableError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    from finengine.skills import run_orchestrator

    async def runner() -> AsyncGenerator[dict, None]:
        async for event in extract_insights_stream(context, model_id=model_id):
            yield event

    run_id = run_orchestrator.start_run(
        runner,
        kind="insights",
        meta={
            "doc_id": doc_id,
            "filename": doc.filename,
            "arch": "insights",
            "model_id": model_id or "",
            "role": requested_role,
            "user_id": getattr(user, "id", None),
        },
    )

    async def event_gen() -> AsyncGenerator[bytes, None]:
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id}, ensure_ascii=False)}\n\n".encode()
        async for chunk in _sse_with_heartbeat(
            run_orchestrator.subscribe(run_id, kind="insights")
        ):
            yield chunk

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/insights/runs/{run_id}/stream")
async def insights_run_resume(
    run_id: str,
    user: User = Depends(get_current_user_detached),
):
    """Re-subscribe to a previously-started Insights run."""
    from finengine.skills import run_orchestrator

    state = run_orchestrator.get_run(run_id)
    if state is not None:
        owner = state.meta.get("user_id")
        if not user.is_admin and owner is not None and owner != user.id:
            raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for chunk in _sse_with_heartbeat(
            run_orchestrator.subscribe(run_id, kind="insights")
        ):
            yield chunk

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# Telegram pairing (in-memory, single-instance). Maps code -> state.
# In production this would live in Redis / DB with a TTL.
_TELEGRAM_PAIRS: dict[str, dict] = {}
_TELEGRAM_TTL_SEC = 600
_TELEGRAM_BOT_USERNAME = "@FinengineBot"


def _telegram_purge() -> None:
    now = time.time()
    dead = [c for c, v in _TELEGRAM_PAIRS.items() if now - v["created_at"] > _TELEGRAM_TTL_SEC and not v.get("linked_user")]
    for c in dead:
        _TELEGRAM_PAIRS.pop(c, None)


@app.post("/integrations/telegram/pair")
def telegram_pair():
    _telegram_purge()
    code = f"{secrets.randbelow(10**6):06d}"
    _TELEGRAM_PAIRS[code] = {"created_at": time.time(), "linked_user": None}
    return {
        "code": code,
        "expires_in_sec": _TELEGRAM_TTL_SEC,
        "bot_username": _TELEGRAM_BOT_USERNAME,
    }


@app.get("/integrations/telegram/status")
def telegram_status(code: str):
    _telegram_purge()
    p = _TELEGRAM_PAIRS.get(code)
    if not p:
        return {"linked": False, "expired": True, "linked_user": None}
    age = time.time() - p["created_at"]
    expired = age > _TELEGRAM_TTL_SEC and not p.get("linked_user")
    return {
        "linked": p["linked_user"] is not None,
        "expired": expired,
        "linked_user": p["linked_user"],
    }


@app.post("/integrations/telegram/confirm")
def telegram_confirm(code: str, user: str):
    """Called by the Telegram bot after the user sends the pair code."""
    p = _TELEGRAM_PAIRS.get(code)
    if not p:
        raise HTTPException(status_code=404, detail="Code not found")
    if time.time() - p["created_at"] > _TELEGRAM_TTL_SEC:
        raise HTTPException(status_code=410, detail="Code expired")
    p["linked_user"] = user
    return {"status": "linked", "linked_user": user}


@app.post("/integrations/telegram/unlink")
def telegram_unlink(code: str):
    _TELEGRAM_PAIRS.pop(code, None)
    return {"status": "unlinked"}


class SynthesisRequest(QueryRequest):
    """Reuses message=str/doc_ids — but we add the metrics list."""
    metrics: list[dict] = []
    period_current: str = ""
    period_prev: str = ""
    company_name: str = ""


class AnalyzeChainsRequest(BaseModel):
    metrics: list[dict] = []
    period_current: str = ""
    period_prev: str = ""
    company_name: str = ""
    model_id: str | None = None
    # Selected agent arch ("aifa_baseline" / "aifa_dev" / …). Baseline runs
    # only the upstream financier-reasoner chains, which are NF-only — for
    # banks it returns a 'unsupported_for_bank' notice instead of producing
    # nonsense by triggering NF chains on overlapping metrics. DEV will host
    # bank-specific chains separately.
    arch: str = ""
    company_type: str = ""
    # "low" | "medium" | "high" — applied to reasoning-capable models only.
    reasoning_effort: str | None = None


def _resolve_company_type(doc: DocumentMeta, body_company_type: str | None) -> str:
    """Resolve "bank" / "non_financial" for a doc.

    Priority:
      1. body.company_type — what the frontend guessed (weak: substring on doc_type).
      2. doc.company_type — what the upload pipeline stored (often UNKNOWN).
      3. detect_company_kind on the .txt sidecar — authoritative bank-marker check.

    `CompanyType` is a `(str, Enum)`; on Python 3.12 `str(member)` returns the
    qualified name, not the value. Read `.value` directly so step 2 doesn't
    leak `"companytype.unknown"` into the candidate list.
    """
    from finengine.skills.aifa.classifier import detect_company_kind

    raw_doc_type = (doc.company_type.value if doc.company_type else "").lower()
    body_type = (body_company_type or "").lower()
    company_type = ""
    for candidate in (body_type, raw_doc_type):
        if candidate and candidate not in {"unknown", ""}:
            company_type = candidate
            break
    if not company_type:
        text_path = settings.docs_dir / f"{doc.doc_id}.txt"
        if text_path.exists():
            try:
                company_type = detect_company_kind(
                    text_path.read_text(encoding="utf-8")[:30000]
                )
            except Exception:
                company_type = ""
    return company_type


@app.post("/documents/{doc_id}/analyze_chains/stream")
async def analyze_chains_stream(
    doc_id: str,
    body: AnalyzeChainsRequest,
    user: User = Depends(get_current_user_detached),
):
    from finengine.llm.openrouter import set_reasoning_effort
    set_reasoning_effort(body.reasoning_effort)
    """Reasoner-chain synthesis (Финансист-style CoT chains). Yields:

    - chain_start / step_start / step_text / step_end / chain_end events
      for each chain that has at least one populated metric
    - summary_start / summary_text / summary_end
    - done

    The frontend renders the events as live markdown sections inside a
    single assistant message — same shape as text_delta but structured.
    """
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    from finengine.skills.reasoner import run_full_synthesis
    from finengine.skills.reasoner.bank_chains import STEP_CONFIG_BANK
    from finengine.skills.reasoner.chains import STEP_CONFIG

    # Decide which chain set to run. The upstream financier-reasoner library
    # only ships NF-company chains (EBITDA, ROA, CapEx, Cash Conversion
    # Cycle, etc.) — running them on a bank piggy-backs on overlapping
    # metrics like TOTAL_ASSETS or NET_INCOME and produces semantically
    # wrong commentary.
    #
    #   AIFA Baseline + bank   → block (the upstream lib has no bank chains)
    #   AIFA DEV    + bank     → use STEP_CONFIG_BANK (NIM, CET1, Stage1/2/3)
    #   any arch    + non-bank → use STEP_CONFIG (upstream NF chains)
    company_type = _resolve_company_type(doc, body.company_type)
    is_bank = company_type == "bank"
    arch = (body.arch or "").lower()
    is_dev = arch in {"aifa_dev"}
    block_for_bank = is_bank and not is_dev

    selected_chains = STEP_CONFIG_BANK if (is_bank and is_dev) else STEP_CONFIG

    # Pre-count chains that will actually run, so the frontend can render a
    # real progress bar instead of guessing N. Mirrors the filter in
    # run_full_synthesis: chains whose step_context_queries hit at least one
    # populated metric.
    populated_enums = {
        m.get("enum")
        for m in body.metrics
        if m.get("value_current") is not None
    }
    chains_planned = 0
    for _key, chain, _title in selected_chains:
        chain_enums: set[str] = set()
        for step in chain:
            chain_enums.update(step.step_context_queries)
        if chain_enums & populated_enums:
            chains_planned += 1

    # ── Resumable background-task pattern ────────────────────────────────
    # Kick off `run_full_synthesis` as an independent asyncio task — when
    # the SSE request that started this gets cancelled (client disconnect
    # / page reload / VPN drop), the chain processing keeps running. The
    # client can reattach via GET /chains/runs/{run_id}/stream and pick
    # up where they left off.
    import time
    from finengine.skills import chain_orchestrator
    from finengine.skills.run_logger import log_chain_run

    head_event = {
        "type": "analysis_start",
        "total_chains": chains_planned,
        "arch": arch or None,
        "is_bank": is_bank,
    }

    # Compose the underlying generator the orchestrator will drive.
    # `block_for_bank` is the only short-circuit case — emit a single
    # `unsupported_for_bank` event and `done`, then exit.
    if block_for_bank:
        _arch_labels = {
            "aifa_baseline": "AIFA Baseline",
            "finengine_baseline": "Finengine Baseline",
            "finengine_rag": "Finengine RAG",
            "finengine_dev": "Finengine DEV",
            "finengine_consensus": "Finengine Triumvirat",
        }
        _arch_label = _arch_labels.get(arch, arch or "текущем")
        async def _runner():
            yield head_event
            yield {
                "type": "unsupported_for_bank",
                "message": (
                    f"Анализ выводов для банков пока не поддерживается "
                    f"в режиме {_arch_label}. Цепочки рассуждений из "
                    f"financier-reasoner написаны для нефинансовых "
                    f"компаний (EBITDA, оборачиваемость, CapEx и т.п.) "
                    f"и применять их к банковской отчётности "
                    f"некорректно. Переключи режим на AIFA DEV — там "
                    f"появятся специализированные банковские цепочки."
                ),
            }
            yield {"type": "done"}
    else:
        async def _runner():
            yield head_event
            try:
                async for ev in run_full_synthesis(
                    metrics=body.metrics,
                    model_id=body.model_id,
                    chains=selected_chains,
                ):
                    yield ev
            except Exception as e:
                yield {"type": "error", "message": str(e)}
                yield {"type": "done"}

    # Audit-log writer fired once the run finishes (success/error/either
    # way). Captures aggregate stats — detailed events are in the
    # per-run journal already.
    async def _on_done(state):
        completed = sum(1 for ev in state.events if ev.get("type") == "chain_end")
        log_chain_run(
            run_id=state.run_id,
            doc_id=doc_id,
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            arch=arch,
            model_id=body.model_id,
            company_type=company_type,
            started_at=state.started_at,
            finished_at=state.finished_at or time.time(),
            chains_planned=chains_planned,
            chains_completed=completed,
            error=state.error,
        )

    run_id = chain_orchestrator.start_run(
        _runner,
        meta={
            "doc_id": doc_id,
            "user_id": getattr(user, "id", None),
            "arch": arch,
            "model_id": body.model_id,
            "company_type": company_type,
        },
        on_done=_on_done,
    )

    async def event_gen() -> AsyncGenerator[bytes, None]:
        # First emit run_id so the frontend can persist it for reattach.
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id}, ensure_ascii=False)}\n\n".encode()
        # Then subscribe to the in-memory + journal stream. Disconnect
        # here doesn't abort the underlying chain task.
        async for ev in chain_orchestrator.subscribe(run_id):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/chains/runs/{run_id}/stream")
async def chain_run_resume_stream(
    run_id: str,
    user: User = Depends(get_current_user_detached),  # noqa: ARG001 (auth gate)
):
    """Reattach to an in-flight (or already-finished) chain analysis.

    Frontend calls this after page reload / VPN drop to resume the SSE
    stream — the underlying chain task on the backend keeps running
    independently of the request that started it (see
    `chain_orchestrator.start_run`). All previously emitted events are
    replayed first (catch-up), then live events are tailed.
    """
    from finengine.skills import chain_orchestrator

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for ev in chain_orchestrator.subscribe(run_id):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


class QuickSynthesisRequest(BaseModel):
    period_current: str = ""
    period_prev: str = ""
    company_name: str = ""
    model_id: str | None = None
    # Hint from FE if it already knows the kind. Resolved server-side via
    # `_resolve_company_type` (FE hint → doc.company_type → text classifier).
    company_type: str = ""
    # "low" | "medium" | "high" — applied to reasoning-capable models only.
    reasoning_effort: str | None = None


@app.post("/documents/{doc_id}/quick_synthesis/stream")
async def quick_synthesis_stream(
    doc_id: str,
    body: QuickSynthesisRequest,
    user: User = Depends(get_current_user_detached),
):
    from finengine.llm.openrouter import set_reasoning_effort
    set_reasoning_effort(body.reasoning_effort)
    """One-pass synthesis directly off the doc text (Finengine quick analysis).

    No extracted-metrics input — the LLM reads the .txt sidecar and writes
    a brief analytical commentary. Used by the 'Быстрый анализ' button in
    Finengine arches when the user wants a take without paying the per-
    metric extract cost.
    """
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    text_path = settings.docs_dir / f"{doc_id}.txt"
    if not text_path.exists():
        raise HTTPException(status_code=400, detail={"code": "document_still_processing", "message": "Документ ещё обрабатывается, подождите."})
    full_text = text_path.read_text(encoding="utf-8")[:200_000]  # cap context

    company = body.company_name or doc.company_name or doc.filename
    period_block = ""
    if body.period_current:
        period_block = f"\nПериоды отчётности: {body.period_current} (тек.)"
        if body.period_prev:
            period_block += f" / {body.period_prev} (пред.)"

    # Bank vs non-financial branching — same classifier the chain endpoint
    # uses. Banks need NIM / CIR / capital adequacy commentary, not EBITDA /
    # CAPEX / FCF (those metrics don't exist in a bank IFRS).
    is_bank = _resolve_company_type(doc, body.company_type) == "bank"
    if is_bank:
        task_block = (
            "## Задача\n"
            "Подготовь развёрнутый аналитический отчёт по финансовому состоянию банка "
            "**объёмом не менее 2000 слов** (~3-4 страницы A4). Это профессиональный обзор "
            "для банковского аналитика инвест-комитета или кредитного риск-офицера, "
            "**не саммари**. Используй ТОЛЬКО цифры из текста отчёта — ничего не выдумывай.\n\n"
            "## Структура (10 разделов, в каждом 2-4 содержательных абзаца):\n\n"
            "### 1. Резюме периода и макроэкономический контекст из отчёта\n"
            "- Главный вывод периода одной фразой плюс три ключевые цифры (Net Income, ROE/ROA, CIR).\n"
            "- Укажи ключевую ставку ЦБ, инфляцию, курс рубля и регуляторные события "
            "только если они прямо упомянуты в отчёте; иначе напиши «не раскрыто».\n\n"
            "### 2. Чистый процентный доход и маржа\n"
            "- NII год-к-году: абсолют, темп роста.\n"
            "- Если есть данные — Net Interest Margin (NIM = NII / средние работающие "
            "активы), её динамика. Норма для банка 2-6%.\n"
            "- ОБЯЗАТЕЛЬНО объясни ПРИЧИНУ изменения NII по компонентам: процентные "
            "доходы по кредитам, по ц.б., процентные расходы по депозитам, по выпущенным "
            "облигациям, по средствам ЦБ. Что изменилось — объёмы или ставки?\n\n"
            "### 3. Кредитный портфель: качество и динамика\n"
            "- Брутто-портфель и его изменение, структура корпорат vs розница.\n"
            "- IFRS-9 stages (Stage 1/2/3): доля Stage 3, как изменилась.\n"
            "- Coverage ratio (ECL Stage 3 / Loans Stage 3) — достаточно ли резервов.\n"
            "- Cost of risk (расходы по ОКУ / средние кредиты) — выше или ниже исторической нормы 1-3%.\n"
            "- Объясни: рост проблемки — это новые дефолты или миграция Stage 2 → 3? "
            "Снижение Stage 3 — реальное улучшение или списания?\n\n"
            "### 4. Структура баланса и ликвидность\n"
            "- Активы: разбивка по классам (касса, кредиты, ц.б., прочее), drift год-к-году.\n"
            "- Доля высоколиквидных активов; покрытие краткосрочных обязательств.\n"
            "- Концентрации: валютная структура, отраслевая, географическая (если есть).\n\n"
            "### 5. Фондирование\n"
            "- Депозиты клиентов: физлица vs юрлица, текущие vs срочные.\n"
            "- Loan-to-Deposits ratio.\n"
            "- Заимствования: межбанк, ЦБ/Минфин, выпущенные облигации, суборды. "
            "Какова стоимость фондирования и её динамика.\n\n"
            "### 6. Капитал и регуляторные нормативы\n"
            "- CET1 / Tier 1 / Total CAR (если раскрыто) vs минимумы ЦБ "
            "(8.5% Tier1, 11.5% Total для системно значимых).\n"
            "- Если норматив НЕ раскрыт в обобщённой отчётности — отметь это явно "
            "вместо выдумывания цифры.\n"
            "- Динамика капитала: прибыль/убыток, взносы акционера, OCI.\n\n"
            "### 7. Комиссионные доходы и небанковская деятельность\n"
            "- Чистые комиссионные доходы: динамика, что разовое vs регулярное.\n"
            "- Прочие операционные доходы / расходы.\n\n"
            "### 8. Операционная эффективность\n"
            "- Cost-to-Income Ratio = (Расходы на персонал и админ) / Operating Income. Норма 40-55%.\n"
            "- Динамика затрат на персонал.\n\n"
            "### 9. Риски и события после отчётной даты\n"
            "- Что банк сам выделил как ключевые риски.\n"
            "- События после отчётной даты — материальны или нет.\n"
            "- Налогообложение и эффективная ставка.\n"
            "- Юридические / санкционные риски (если упомянуты).\n\n"
            "### 10. Итоговая оценка и прогноз\n"
            "- Финансовая устойчивость: 3 главных позитива и 3 главных риска (с подтверждением числом).\n"
            "- Что бы ты как аналитик отметил для инвест-комитета?\n"
            "- На какие leading indicators смотреть в следующем периоде.\n\n"
            "## Жёсткие правила\n"
            "- НЕ ИЗОБРЕТАЙ цифры. Если данных нет — пиши «не раскрыто» и объясни, почему это важно.\n"
            "- Не дополняй макроконтекст внешними или актуальными на сегодня данными: "
            "анализируй только то, что явно раскрыто в документе.\n"
            "- ЗАПРЕТ на спекуляции: если из таблицы нельзя извлечь причину явления — НЕ "
            "используй «по всей видимости», «вероятно», «возможно», «скорее всего». "
            "Пиши «по имеющимся данным атрибуция причины невозможна, нужны [список]».\n"
            "- В каждом разделе кроме самой цифры давай ВЫВОД и КОНТЕКСТ.\n"
            "- Сравнивай с предыдущим периодом везде, где возможно, и объясняй ПРИЧИНУ.\n"
            "- Используй банковскую терминологию (NIM, NPL, CIR, CAR, Tier1, LCR, NSFR, "
            "cost of funds, pre-provision profit) корректно.\n"
            "- Без markdown-таблиц, без формул в тексте (но цифры выписывай чётко).\n"
            "- Числа в **млрд руб.** или **млн руб.**, как в отчёте; проценты с одной десятой (16.9%, не 17%).\n"
            "- Финальный объём — **не менее 2000 слов** (жёсткое требование, не пожелание)."
        )
    else:
        task_block = (
            "## Задача\n"
            "Подготовь развёрнутый аналитический отчёт по финансовому состоянию компании "
            "**объёмом не менее 2000 слов** (~3-4 страницы A4). Это профессиональный обзор "
            "для инвест-комитета или кредитного аналитика, **не саммари**. Используй ТОЛЬКО "
            "цифры из текста отчёта — ничего не выдумывай.\n\n"
            "## Структура (10 разделов, в каждом 2-4 содержательных абзаца):\n\n"
            "### 1. Резюме периода и макроэкономический контекст из отчёта\n"
            "- Главный вывод периода одной фразой плюс три ключевые цифры "
            "(Net Income, EBITDA margin, ROE).\n"
            "- Укажи ключевую ставку ЦБ, инфляцию, курс рубля и регуляторные / отраслевые "
            "события только если они прямо упомянуты в отчёте; иначе напиши «не раскрыто».\n\n"
            "### 2. Выручка и операционная маржа\n"
            "- Динамика выручки год-к-году: абсолют, темп роста, объясни ПРИЧИНУ "
            "(объёмы / цены / структура продуктов).\n"
            "- Себестоимость, валовая маржа, операционная маржа, EBITDA margin — динамика.\n"
            "- Прокомментируй: компания нарастила объёмы или подняла цены? "
            "Удержала маржу или маржа сжалась?\n\n"
            "### 3. Чистая прибыль и её качество\n"
            "- Net Income и его драйверы. Доля разовых статей (impairment, "
            "переоценка, F/X) vs органическая прибыль.\n"
            "- Доля в прибыли ассоциированных и СП.\n"
            "- Эффективная ставка налога — необычные эффекты.\n\n"
            "### 4. Денежные потоки\n"
            "- Operating Cash Flow vs Net Income — gap и его интерпретация "
            "(работа с оборотным капиталом, амортизация, изменение резервов).\n"
            "- Capex — net и gross, интенсивность к выручке (%).\n"
            "- Free Cash Flow = OCF - CAPEX.\n"
            "- Dividends paid, выкуп акций, M&A — куда направлен FCF.\n\n"
            "### 5. Долговая нагрузка и ликвидность\n"
            "- Net Debt = Total Debt - Cash, Net Debt/EBITDA (норма 1.5-3.0× для "
            "большинства отраслей, до 4× для капиталоёмких).\n"
            "- Interest Coverage = EBITDA / Interest Expense (норма ≥3×).\n"
            "- Структура долга: краткосрочный vs долгосрочный, валютный профиль.\n"
            "- Ликвидность: Current Ratio, Quick Ratio.\n\n"
            "### 6. Оборотный капитал и эффективность\n"
            "- Оборачиваемость дебиторки, кредиторки, запасов (DSO, DPO, DIO).\n"
            "- Cash Conversion Cycle.\n"
            "- Изменения в оборотном капитале как отрицательный/положительный фактор для FCF.\n\n"
            "### 7. CAPEX и инвестиционная активность\n"
            "- CAPEX intensity (Capex / Revenue) — выше или ниже амортизации? "
            "(если ≥, компания растёт; если ≤, компания «сжимается»).\n"
            "- M&A: приобретения дочерних, ассоциированных.\n"
            "- Регулярность инвестиций — стратегический выбор.\n\n"
            "### 8. Структура капитала и собственники\n"
            "- Total Equity и его динамика, долговая структура vs капитал.\n"
            "- Дивидендная политика, выкуп акций.\n"
            "- Доля неконтролирующих участников.\n\n"
            "### 9. Риски и события после отчётной даты\n"
            "- Главные риски, выделенные самой компанией в отчёте.\n"
            "- Материальные события после отчётной даты.\n"
            "- Юридические / санкционные / экологические риски (если упомянуты).\n\n"
            "### 10. Итоговая оценка и прогноз\n"
            "- 3 главных позитива и 3 главных риска (с подтверждением числом).\n"
            "- Финансовая устойчивость и инвест-привлекательность: общий вывод с "
            "однозначной рекомендацией («устойчиво / умеренный риск / повышенный риск»).\n"
            "- Leading indicators для следующего периода.\n\n"
            "## Жёсткие правила\n"
            "- НЕ ИЗОБРЕТАЙ цифры. Если данных нет — пиши «не раскрыто» и объясни, почему важно.\n"
            "- Не дополняй макроконтекст внешними или актуальными на сегодня данными: "
            "анализируй только то, что явно раскрыто в документе.\n"
            "- В каждом разделе цифра + ВЫВОД + КОНТЕКСТ.\n"
            "- Сравнивай с предыдущим периодом везде где возможно, и объясняй ПРИЧИНУ.\n"
            "- Используй финансовую терминологию (EBITDA margin, FCF, CAPEX intensity, ROIC, "
            "working capital turnover, Net Debt/EBITDA, interest coverage) корректно.\n"
            "- Без markdown-таблиц, без формул в тексте.\n"
            "- Числа в **млрд руб.** или **млн руб.**, как в отчёте; проценты с одной десятой.\n"
            "- Финальный объём — **не менее 2000 слов** (жёсткое требование, не пожелание)."
        )

    prompt = (
        f"# Финансовый отчёт компании {company}{period_block}\n\n"
        f"## Текст отчёта\n{full_text}\n\n"
        f"{task_block}"
    )

    from finengine.llm import openrouter as llm
    from finengine.skills.run_logger import log_quick_synthesis
    from finengine.skills import run_orchestrator

    started_at = time.time()
    capture: dict = {"text": "", "error": None}

    async def runner() -> AsyncGenerator[dict, None]:
        try:
            text, _usage = await llm.complete_with_usage(
                prompt=prompt,
                # The task block above is written in Russian; the directive is
                # what decides the language the report comes back in.
                system=language_directive(),
                model=body.model_id,
                # 2000+ слов аналитического текста ≈ 3000-4000 токенов; беру 6000
                # с запасом, чтобы модель не урезала вывод и могла дописать
                # все 10 разделов структуры. Раньше стоял cap 2000, который
                # буквально отрубал ответ на середине.
                max_output=6000,
            )
            capture["text"] = (text or "").strip()
            yield {"type": "text_delta", "text": capture["text"]}
        except Exception as e:
            capture["error"] = str(e)
            yield {"type": "error", "message": capture["error"]}
        yield {"type": "done"}

    async def _on_done(state: run_orchestrator.RunState) -> None:
        try:
            log_quick_synthesis(
                doc_id=doc_id,
                doc_filename=doc.filename,
                company_name=company,
                company_type="bank" if is_bank else "non_financial",
                arch="finengine",
                model_id=body.model_id,
                user_id=user.id,
                username=user.username,
                started_at=started_at,
                response=capture["text"],
                error=capture["error"] or state.error,
            )
        except Exception:
            pass

    run_id = run_orchestrator.start_run(
        runner,
        kind="quick",
        meta={
            "doc_id": doc_id,
            "filename": doc.filename,
            "user_id": getattr(user, "id", None),
            "model_id": body.model_id or "",
        },
        on_done=_on_done,
    )

    async def event_gen() -> AsyncGenerator[bytes, None]:
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id}, ensure_ascii=False)}\n\n".encode()
        async for ev in run_orchestrator.subscribe(run_id, kind="quick"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/quick/runs/{run_id}/stream")
async def quick_run_resume(
    run_id: str,
    user: User = Depends(get_current_user_detached),
):
    """Re-subscribe to a previously-started quick_synthesis run.

    Mirror of /extract/runs/{run_id}/stream — same auth check + same
    journal-replay semantics.
    """
    from finengine.skills import run_orchestrator
    state = run_orchestrator.get_run(run_id)
    if state is not None:
        owner = state.meta.get("user_id")
        if not user.is_admin and owner is not None and owner != user.id:
            raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for ev in run_orchestrator.subscribe(run_id, kind="quick"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.post("/documents/{doc_id}/synthesize/stream")
async def synthesize_stream_endpoint(
    doc_id: str,
    body: SynthesisRequest,
    user: User = Depends(get_current_user_detached),
):
    """Streaming analytical commentary over already-extracted metrics."""
    doc = index.get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not _user_can_see(doc, user):
        raise HTTPException(status_code=404, detail="Document not found")

    from finengine.skills.synthesis import synthesize_stream
    owner_id = None if user.is_admin else user.id

    async def event_gen() -> AsyncGenerator[bytes, None]:
        try:
            async for delta in synthesize_stream(
                index=index,
                doc_id=doc_id,
                owner_id=owner_id,
                metrics=body.metrics,
                period_current=body.period_current,
                period_prev=body.period_prev,
                company_name=body.company_name,
            ):
                yield f"data: {json.dumps({'type': 'text_delta', 'text': delta}, ensure_ascii=False)}\n\n".encode()
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n".encode()
        yield b"data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


class WebSearchRequest(BaseModel):
    query: str
    max_results: int = 5


@app.post("/web_search")
async def web_search_endpoint(
    body: WebSearchRequest,
    user: User = Depends(get_current_user),
):
    """Tavily-backed web search used by Finengine DEV when the user wants
    to look something up beyond the document. Returns 5 hits + the query.
    Empty results + a friendly message if the Tavily key isn't configured.
    """
    from finengine.skills.web_search import is_enabled, web_search

    if not is_enabled():
        return {
            "enabled": False,
            "results": [],
            "message": (
                "Поиск в сети не настроен на этом инстансе. "
                "Заведи API-ключ Tavily и пропиши FINENGINE_TAVILY_API_KEY."
            ),
        }
    try:
        hits = await web_search(body.query, max_results=body.max_results)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Web search failed: {e}")
    return {
        "enabled": True,
        "results": [
            {"title": h.title, "url": h.url, "content": h.content, "score": h.score}
            for h in hits
        ],
    }


@app.post("/query")
async def query(request: QueryRequest, user: User = Depends(get_current_user)):
    """Query the agent (non-streaming). Owner-scoped FAISS search."""
    agent = get_agent()
    owner_id = None if user.is_admin else user.id
    result = agent.query(
        message=request.message,
        session_id=request.session_id,
        doc_ids=request.doc_ids or None,
        owner_id=owner_id,
    )
    return QueryResponse(
        session_id=result["session_id"],
        answer=result["answer"],
        sources=result["sources"],
    )


@app.post("/query/stream")
async def query_stream(request: QueryRequest, user: User = Depends(get_current_user_detached)):
    """Query the agent with streaming response. Owner-scoped FAISS search.

    Resumable: spawns the agent run as an asyncio.create_task that outlives
    this request. Page reload / VPN drop cancels the SSE stream but the
    worker keeps going; the frontend reattaches via /chat/runs/{id}/stream.
    """
    enforce_spend_budget(user)
    from finengine.skills.run_logger import log_chat_turn
    from finengine.skills import run_orchestrator
    import time as _t

    agent = get_agent()
    owner_id = None if user.is_admin else user.id
    started_at = _t.time()

    capture: dict = {
        "response": [],
        "session_id": request.session_id or "",
        "error": None,
    }

    is_aifa_baseline = request.arch == "aifa_baseline"
    disclaimer = t("chat.pilotDisclaimer") if is_aifa_baseline else None

    async def runner() -> AsyncGenerator[dict, None]:
        if disclaimer:
            capture["response"].append(disclaimer)
            yield {"type": "text_delta", "text": disclaimer}
        try:
            async for event in agent.query_stream(
                message=request.message,
                session_id=request.session_id,
                doc_ids=request.doc_ids or None,
                owner_id=owner_id,
            ):
                if event.get("type") == "session_id":
                    capture["session_id"] = event.get("session_id", capture["session_id"])
                elif event.get("type") == "text_delta":
                    capture["response"].append(event.get("text", ""))
                yield event
        except Exception as e:
            capture["error"] = f"{type(e).__name__}: {e}"
            yield {"type": "error", "message": capture["error"]}

    async def _on_done(state: run_orchestrator.RunState) -> None:
        try:
            log_chat_turn(
                session_id=capture["session_id"],
                user_id=user.id,
                username=user.username,
                doc_ids=request.doc_ids or [],
                message=request.message,
                response="".join(capture["response"]),
                started_at=started_at,
                arch=request.arch,
                error=capture["error"] or state.error,
            )
        except Exception:
            pass

    run_id = run_orchestrator.start_run(
        runner,
        kind="chat",
        meta={
            "session_id": capture["session_id"],
            "doc_ids": request.doc_ids or [],
            "arch": request.arch,
            "user_id": getattr(user, "id", None),
        },
        on_done=_on_done,
    )

    async def event_stream() -> AsyncGenerator[bytes, None]:
        # Frontend persists run_id so a reload/VPN drop reattaches via
        # GET /chat/runs/{run_id}/stream instead of restarting the turn.
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': run_id}, ensure_ascii=False)}\n\n".encode()
        async for ev in run_orchestrator.subscribe(run_id, kind="chat"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/chat/runs/{run_id}/stream")
async def chat_run_resume(
    run_id: str,
    user: User = Depends(get_current_user_detached),
):
    """Re-subscribe to a previously-started chat turn.

    Same auth + journal-replay semantics as /extract/runs and /chains/runs.
    """
    from finengine.skills import run_orchestrator
    state = run_orchestrator.get_run(run_id)
    if state is not None:
        owner = state.meta.get("user_id")
        if not user.is_admin and owner is not None and owner != user.id:
            raise HTTPException(status_code=404, detail="Run not found")

    async def event_gen() -> AsyncGenerator[bytes, None]:
        async for ev in run_orchestrator.subscribe(run_id, kind="chat"):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── Per-user dialog persistence ────────────────────────────────────────
# Browser localStorage holds the canonical project state today, so a cache
# wipe / new device wipes the chat history. These endpoints store a single
# JSON blob per user (the same shape the frontend already builds locally)
# so the client can fetch on login and survive resets.
#
# We keep the storage extremely simple — one file per user, last-write-wins.
# No per-project endpoints, no diff sync. Acceptable while one user is one
# device most of the time. If multi-tab divergence becomes a real issue,
# revisit with a CRDT or a project-level revision counter.
_DIALOGS_DIR = settings.data_dir / "dialogs"
_DIALOGS_MAX_BYTES = 8_000_000  # 8 MB; trips at ~thousands of long messages
# Data-loss backstop: a stale or fresh client (1-few projects) must never be
# able to overwrite a populated server blob. Once the server holds at least
# _MIN_EXISTING projects, reject a PUT carrying fewer than _MIN_INCOMING.
# See the 2026-06 dialog-overwrite incidents.
_DIALOGS_GUARD_MIN_EXISTING = 20
_DIALOGS_GUARD_MIN_INCOMING = 5
_DIALOGS_WRITE_LOCK = threading.Lock()


def _user_dialog_path(user_id: int) -> Path:
    return _DIALOGS_DIR / f"{user_id}.json"


def _dialog_project_count(path: Path) -> int | None:
    """Number of projects in the on-disk blob, or None if absent/unreadable."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    projs = existing.get("projects") if isinstance(existing, dict) else None
    return len(projs) if isinstance(projs, list) else None


def _decompress_gzip_capped(data: bytes, cap: int) -> bytes:
    """Gunzip ``data`` but never materialise more than ``cap`` bytes.

    The dialog blob is multi-MB, so the client sends it gzip-encoded
    (~8× smaller upload). Decompress with a hard output cap so a crafted
    body can't amplify into an OOM on the single worker.
    """
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16 => expect gzip header
    try:
        out = dec.decompress(data, cap + 1)  # caps output -> bomb-safe
        out += dec.flush()
    except zlib.error:
        raise HTTPException(status_code=400, detail="Malformed gzip body")
    if len(out) > cap:
        raise HTTPException(
            status_code=413,
            detail="Dialog state too large (>8 MB). Удалите старые проекты или сообщения.",
        )
    return out


@app.get("/dialogs")
def get_dialogs(user: User = Depends(get_current_user)):
    """Return the user's saved dialog state. Empty payload on first access."""
    path = _user_dialog_path(user.id)
    if not path.exists():
        return {"projects": [], "last_project_id": None, "saved_at": None}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # The file EXISTS but couldn't be read/parsed (transient FS hiccup,
        # a mid-write read, or an older writer that left it mid-utf-8). Do NOT
        # return an empty state here: the client treats "no projects" as a
        # fresh server and uploads its local (possibly near-empty) copy,
        # overwriting the real data — the 2026-06 overwrite incidents. Signal
        # 503 instead so the client sees a failed GET (server === null) and
        # leaves sync disabled rather than clobbering the saved chats.
        raise HTTPException(status_code=503, detail="Dialog store temporarily unavailable")


@app.put("/dialogs")
async def put_dialogs(
    request: Request,
    content_encoding: str | None = Header(default=None),
    x_dialog_owner: str | None = Header(default=None),
    user: User = Depends(get_current_user),
):
    """Upsert the user's dialog state. Whole-blob replace. Capped at 8 MB.

    The client owns the schema — backend just stores and returns it
    verbatim. We tag the saved_at server-side so the client can show a
    "synced X ago" indicator without trusting its own clock.

    The body is raw JSON bytes, optionally ``Content-Encoding: gzip`` (the
    blob is multi-MB; compressing the upload turns a ~30 s PUT on the
    single worker into a sub-second one). We read the raw request body so
    any content-type works — older clients still PUT plain application/json.
    Parsing + the locked write run in a threadpool so the event loop (and
    in-flight SSE streams) stay responsive.
    """
    # Identity guard. The session cookie is shared by every tab, so a tab left
    # open under one account keeps that account's chats in memory and will
    # happily PUT them after someone else logs in — the cookie says "B", the
    # payload is A's. On 2026-07-30 that filed an admin's 102 chats under a
    # freshly created account. The client names the owner it *believes* it is
    # writing for; a mismatch is a bug, never a legitimate save.
    # Header absent → older client, allowed (kept for rollout compatibility).
    if x_dialog_owner and x_dialog_owner != user.username:
        raise HTTPException(
            status_code=409,
            detail="Dialog owner mismatch — reload the page (session changed).",
        )
    raw = await request.body()
    return await run_in_threadpool(_store_dialogs, raw, content_encoding, user.id)


def _store_dialogs(raw: bytes, content_encoding: str | None, user_id: int) -> dict:
    if (content_encoding or "").lower() == "gzip":
        raw = _decompress_gzip_capped(raw, _DIALOGS_MAX_BYTES)
    if len(raw) > _DIALOGS_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Dialog state too large (>8 MB). Удалите старые проекты или сообщения.",
        )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed dialog payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Dialog payload must be an object")
    payload["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    incoming = payload.get("projects")
    incoming_count = len(incoming) if isinstance(incoming, list) else 0
    path = _user_dialog_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.tmp.{threading.get_ident()}")
    with _DIALOGS_WRITE_LOCK:
        existing_count = _dialog_project_count(path)
        if (
            existing_count is not None
            and existing_count >= _DIALOGS_GUARD_MIN_EXISTING
            and incoming_count < _DIALOGS_GUARD_MIN_INCOMING
        ):
            print(
                f"[dialogs] BLOCKED overwrite for user {user_id}: "
                f"{existing_count} projects -> {incoming_count}",
                flush=True,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "Refusing to replace saved chats with a near-empty state. "
                    "Reload the page to fetch the server copy."
                ),
            )
        # One-step snapshot of the prior state before replacing it, so any
        # unexpected loss is instantly recoverable without the daily backup.
        if path.exists():
            try:
                shutil.copy2(path, path.with_suffix(".json.prev"))
            except OSError:
                pass
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
            tmp.replace(path)
            return {"ok": True, "saved_at": payload["saved_at"]}
        finally:
            tmp.unlink(missing_ok=True)
@app.delete("/dialogs")
def delete_dialogs(user: User = Depends(get_current_user)):
    """Wipe the user's saved dialog state — for the 'Clear server history'
    button if/when we add one. Local localStorage is unaffected."""
    path = _user_dialog_path(user.id)
    with _DIALOGS_WRITE_LOCK:
        if path.exists():
            path.unlink()
    return {"ok": True}


# ── User feedback ────────────────────────────────────────────────────────
# Floating "Нашли ошибку?" button on every page → modal with a textarea →
# POST /feedback. Captured per-user with the page URL and any active doc/run
# so testers can flag bad extractions or wrong analysis values without leaving
# the screen. Stored as JSONL (append-only), readable via /admin/feedback.
_FEEDBACK_PATH = settings.docs_dir / "feedback.jsonl"


@app.post("/feedback")
def post_feedback(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
):
    """Append a feedback entry. Body fields (all optional except `text`):
      text       — the user's note (required, ≤4000 chars)
      page_url   — full URL where the user was when they clicked
      doc_id     — currently-open document, if any
      run_id     — currently-open extract / chains / quick run, if any
      context    — any other JSON-serialisable hints from the frontend
    """
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 4000:
        raise HTTPException(status_code=413, detail="text too long (>4000 chars)")
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user_id": user.id,
        "username": user.username,
        "text": text,
        "page_url": (payload.get("page_url") or "")[:500],
        "doc_id": (payload.get("doc_id") or "")[:64],
        "run_id": (payload.get("run_id") or "")[:64],
        "context": payload.get("context") or {},
    }
    _FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/admin/feedback")
def get_feedback(user: User = Depends(get_current_user)):
    """Return all feedback entries (newest first). Admin only."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    if not _FEEDBACK_PATH.exists():
        return {"entries": []}
    entries = []
    with open(_FEEDBACK_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.reverse()
    return {"entries": entries}
