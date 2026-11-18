"""Authenticated, idempotent registration of turns originating in Dialogs.

The agent already runs in GigaAgent: registration must not start a second agent
through /chat/turns. Documents and receipts persist in the existing AIFA stores;
the active-turn lease uses the same six-hour coordination TTL as browser turns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from finengine.assistant.tool_api import token_refusal
from finengine.assistant.turn_record import TTL_SEC, TurnRecord, recall, remember
from finengine.auth.db import SessionLocal, User
from finengine.ingestion.document_store import get_store
from finengine.models.document import DocumentMeta
from finengine.store import blobs, state
from finengine.store.cache import get_cache

log = logging.getLogger(__name__)
router = APIRouter()
MAX_BYTES = 50 * 1024 * 1024  # Same total input bound as /documents/upload.


class DialogsTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, max_length=255)
    agent_id: str = Field(min_length=1, max_length=255)
    chat_id: str = Field(min_length=1, max_length=255)
    source_message_id: str = Field(min_length=1, max_length=255)
    message: str = Field(max_length=100_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class Registration:
    record: TurnRecord
    pending: tuple[DocumentMeta, ...] = ()


def owner_user(identity: str) -> int:
    """Stable non-login service user, isolated by owner, agent and Dialogs chat."""
    username = "dialogs-" + identity[:48]
    with SessionLocal() as session:
        found = session.scalar(select(User).where(User.username == username))
        if found is None:
            found = User(username=username, password_hash="!", is_admin=False)
            session.add(found)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                found = session.scalar(select(User).where(User.username == username))
                if found is None:
                    raise
        return found.id


def register_turn(turn: DialogsTurn, files: list[tuple[str, bytes]], *, trace_id: str = "") -> Registration:
    if len(files) != len(turn.attachment_ids) or len(set(turn.attachment_ids)) != len(turn.attachment_ids):
        raise HTTPException(422, "Attachment identities and file parts must match")
    if sum(len(content) for _, content in files) > MAX_BYTES:
        raise HTTPException(413, "Report attachments exceed the input size limit")
    scope = json.dumps([turn.owner_id, turn.agent_id, turn.chat_id])
    identity = hashlib.sha256(scope.encode()).hexdigest()
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["dialogs", identity, turn.source_message_id])).hex
    fingerprint = hashlib.sha256(json.dumps([
        turn.message, turn.attachment_ids,
        [(name, hashlib.sha256(content).hexdigest()) for name, content in files],
    ]).encode()).hexdigest()
    user_id = owner_user(identity)
    receipt = state.get("dialogs_turn", owner=user_id, key=run_id)
    if receipt is not None and receipt["fingerprint"] != fingerprint:
        raise HTTPException(409, "The Dialogs message was already registered with different input")
    cached = recall(run_id)
    if cached is not None:
        return Registration(cached)
    store = get_store()
    if receipt is None:
        doc_ids = []
        for file_id, (raw_name, content) in zip(turn.attachment_ids, files, strict=True):
            filename = PurePosixPath(raw_name.replace("\\", "/")).name
            suffix = PurePosixPath(filename).suffix.lower()
            if suffix not in {".pdf", ".txt", ".md"} or (suffix == ".pdf" and not content[:1024].lstrip().startswith(b"%PDF-")):
                raise HTTPException(415, "Unsupported or invalid report attachment")
            doc_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["dialogs-document", identity, file_id])).hex
            digest = hashlib.sha256(content).hexdigest()
            meta = store.get_document(doc_id)
            if meta is not None and (meta.owner_id != user_id or meta.sha256 != digest):
                raise HTTPException(409, "The report identity is already bound to different content")
            if meta is None:
                key = blobs.document_key(doc_id, filename)
                blobs.get_blob_store().put(key, content)
                meta = DocumentMeta(doc_id=doc_id, filename=filename, owner_id=user_id,
                                    file_path=key, sha256=digest, ocr_engine="idp" if suffix == ".pdf" else "pymupdf")
                store.add_document(meta, [])
            doc_ids.append(doc_id)
        if not files:
            previous = state.get("dialogs_chat", owner=user_id, key=identity) or {}
            doc_ids = previous.get("doc_ids", [])
        record = TurnRecord(run_id=run_id, owner_id=turn.owner_id, agent_id=turn.agent_id,
                            chat_id=turn.chat_id, message=turn.message, user_id=user_id,
                            case_id="dialogs-" + identity, doc_ids=tuple(doc_ids),
                            active_doc_id=doc_ids[0] if len(doc_ids) == 1 else "", role="", trace_id=trace_id)
        receipt = state.claim("dialogs_turn", owner=user_id, key=run_id,
                              doc={"fingerprint": fingerprint, "record": asdict(record)})
        if receipt["fingerprint"] != fingerprint:
            raise HTTPException(409, "Conflicting Dialogs registration")
        if files:
            state.put("dialogs_chat", owner=user_id, key=identity, doc={"doc_ids": doc_ids})
    record = TurnRecord(**{**receipt["record"], "doc_ids": tuple(receipt["record"]["doc_ids"])})
    pending = []
    for doc_id in record.doc_ids:
        meta = store.get_document(doc_id)
        if meta is None or meta.owner_id != user_id:
            raise HTTPException(409, "A registered report is no longer available to this owner")
        if not blobs.document_text(doc_id):
            pending.append(meta)
    # No second agent is started. Tool jobs need only this active-run lease;
    # each new Dialogs message has its own run, so it cannot block the next one.
    get_cache().set(f"run:{run_id}", {"kind": "chat", "finished": False, "owner_id": user_id}, TTL_SEC)
    remember(record)
    log.info("event=aifa_dialogs_turn_registered run_id=%s documents=%d parse_pending=%d",
             run_id, len(record.doc_ids), len(pending))
    return Registration(record, tuple(pending))


def _parse_document(meta: DocumentMeta) -> None:
    from finengine.api.main import _parse_doc_in_background
    _parse_doc_in_background(meta.file_path, meta.doc_id, meta.filename, meta.ocr_engine)


@router.post("/internal/dialogs/v1/turns")
async def register_dialogs_turn(request: Request, background: BackgroundTasks,
                                authorization: str = Header("")):
    refused = token_refusal(authorization)
    if refused is not None:
        return PlainTextResponse(refused.text, status_code=refused.status)
    async with request.form(max_files=10) as form:
        try:
            turn = DialogsTurn.model_validate_json(form.get("registration", ""))
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(422, "Invalid Dialogs registration") from None
        files = []
        remaining = MAX_BYTES
        for upload in form.getlist("files"):
            if not hasattr(upload, "read"):
                raise HTTPException(422, "Expected report file parts")
            content = await upload.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                raise HTTPException(413, "Report attachments exceed the input size limit")
            files.append((upload.filename or "", content))
        result = await asyncio.to_thread(register_turn, turn, files, trace_id=request.headers.get("X-Trace-Id", ""))
    for meta in result.pending:
        background.add_task(_parse_document, meta)
    return JSONResponse({"run_id": result.record.run_id, "doc_ids": list(result.record.doc_ids)})
