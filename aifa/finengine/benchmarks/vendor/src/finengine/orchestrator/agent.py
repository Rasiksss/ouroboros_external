"""Finengine orchestrator. LLM routed via OpenRouter (GLM-5.1 default)."""
from __future__ import annotations
import logging
import uuid
from typing import AsyncGenerator

from finengine.i18n import scaffold, t

logger = logging.getLogger(__name__)

from finengine.llm import openrouter as llm
from finengine.orchestrator.system_prompt import system_prompt
from finengine.skills.extractor import get_index
from finengine.skills.synthesizer import synthesize
from finengine.skills.factor_analyst import factor_analyze
from finengine.skills.visualizer import create_chart


def _build_context(
    message: str,
    doc_ids: list[str] | None,
    owner_id: int | None = None,
) -> tuple[str, list[dict]]:
    """Full-context: load whole text of each selected doc from .txt sidecar.

    Falls back to FAISS-search snippets if the .txt is missing (legacy docs that
    were uploaded before the full-context migration).
    """
    from pathlib import Path
    from finengine.config import settings

    index = get_index()
    if not doc_ids:
        return "", []

    marks = scaffold()
    parts: list[str] = []
    sources: list[dict] = []

    for doc_id in doc_ids:
        meta = index.get_document(doc_id)
        if not meta:
            continue
        if owner_id is not None and meta.owner_id and meta.owner_id != owner_id:
            continue

        txt_path = Path(settings.docs_dir) / f"{doc_id}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8")
            label = meta.company_name or meta.filename
            parts.append(f"{marks['document_header'].format(label=label)}\n\n{text}")
            sources.append({"filename": meta.filename, "company_name": meta.company_name})
        else:
            # Legacy doc — fall back to FAISS retrieval for it.
            res = index.search(message, top_k=8, doc_ids=[doc_id], owner_id=owner_id)
            if res:
                snippet = "\n\n---\n\n".join(f"[{r.source_ref}]\n{r.content}" for r in res)
                header = marks["document_partial_header"].format(label=meta.filename)
                parts.append(f"{header}\n\n{snippet}")
                sources.append({"filename": meta.filename, "company_name": meta.company_name})

    return "\n\n========\n\n".join(parts), sources


class Session:
    def __init__(self):
        self.session_id = str(uuid.uuid4())
        self.history: list[dict] = []  # {"role": "user"|"assistant", "content": str}
        self.doc_ids: list[str] = []

    def add(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def format_history(self) -> str:
        if len(self.history) <= 2:
            return ""
        marks = scaffold()
        lines = []
        for msg in self.history[:-1]:  # exclude last user message
            prefix = marks["role_user"] if msg["role"] == "user" else marks["role_assistant"]
            lines.append(f"{prefix}: {msg['content'][:500]}")
        return "\n".join(lines)


class FinengineAgent:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    def get_or_create_session(self, session_id: str = "") -> Session:
        if session_id and session_id in self.sessions:
            return self.sessions[session_id]
        session = Session()
        self.sessions[session.session_id] = session
        return session

    async def query_stream(
        self,
        message: str,
        session_id: str = "",
        doc_ids: list[str] | None = None,
        owner_id: int | None = None,
    ) -> AsyncGenerator[dict, None]:
        session = self.get_or_create_session(session_id)
        if doc_ids:
            session.doc_ids = doc_ids

        yield {"type": "session_id", "session_id": session.session_id}

        # 0. If the user attached PDFs that aren't parsed yet (.txt sidecar
        # not on disk), emit `thinking` events while we wait for the
        # BackgroundTask to finish — instead of asking the user to retry.
        # `thinking` is a transient single-line indicator on the frontend
        # (replace, not append); when the real answer starts streaming it
        # disappears, so the final message stays clean.
        # Most reports parse in 5-30s; hard timeout 5 min as a safety net.
        if session.doc_ids:
            from pathlib import Path
            from finengine.config import settings as _settings
            import asyncio
            import time as _t
            pending = [
                d for d in session.doc_ids
                if not (Path(_settings.docs_dir) / f"{d}.txt").exists()
            ]
            if pending:
                # (wire code, ru text). `code` is what the frontend localizes
                # via i18n/stream.ts; `text` is kept so a frontend that
                # predates the code path still shows something.
                phases = [
                    ("uploading", "📄 Загружаю документ…"),
                    ("parsing_pdf", "🔎 Парсю PDF и распознаю таблицы…"),
                    ("indexing", "📑 Индексирую страницы…"),
                    ("preparing_context", "⚙️ Готовлю контекст…"),
                ]
                yield {"type": "thinking", "code": phases[0][0], "text": phases[0][1]}
                deadline = _t.time() + 300
                next_phase_at = _t.time() + 6
                phase_idx = 1
                while pending:
                    if _t.time() > deadline:
                        yield {"type": "text_delta", "text": t("chat.pdfTimeout")}
                        yield {"type": "done"}
                        return
                    await asyncio.sleep(1.5)
                    pending = [
                        d for d in pending
                        if not (Path(_settings.docs_dir) / f"{d}.txt").exists()
                    ]
                    if pending and _t.time() >= next_phase_at and phase_idx < len(phases):
                        code, text = phases[phase_idx]
                        yield {"type": "thinking", "code": code, "text": text}
                        phase_idx += 1
                        next_phase_at = _t.time() + 6

        # 1. Build full-context: whole text of each selected document.
        context, sources = _build_context(message, session.doc_ids or None, owner_id=owner_id)

        # Token guard: rough estimate (4 chars ≈ 1 token, ×1.95 for Cyrillic
        # in non-English tokenizers). Hard ceiling 800K real tokens to leave
        # headroom inside Qwen's 1M window.
        approx_real_tokens = int(len(context) / 4 * 1.95)
        if approx_real_tokens > 800_000:
            err = t("chat.tooManyDocs", tokens=approx_real_tokens // 1000)
            yield {"type": "text_delta", "text": err}
            yield {"type": "done"}
            return

        # 2. Build full prompt
        history_text = session.format_history()
        marks = scaffold()
        prompt_parts = []

        if history_text:
            prompt_parts.append(f"{marks['history_header']}\n{history_text}")

        if context:
            prompt_parts.append(f"{marks['documents_header']}\n{context}")
        elif session.doc_ids:
            # Defensive: doc_ids is non-empty (user attached PDFs) but
            # _build_context returned nothing. We DON'T tell the model
            # to apologize — the frontend's handleSend already prepends
            # extracted-metric tables into the user's `message` when any
            # are available, so the model has plenty of data to answer
            # on. Adding a "PDF не извлёкся" instruction here was making
            # the model wrongly refuse to use those metrics.
            #
            # Diagnostic log so we can investigate the root cause when
            # this fires (txt missing? owner_id mismatch? meta gone?).
            try:
                from pathlib import Path as _P
                from finengine.config import settings as _s
                from finengine.skills.extractor import get_index as _gi
                _idx = _gi()
                diag_lines = []
                for d in session.doc_ids:
                    meta = _idx.get_document(d)
                    txt = (_P(_s.docs_dir) / f"{d}.txt").exists()
                    if not meta:
                        diag_lines.append(f"{d[:8]}=no_meta")
                    elif (
                        owner_id is not None
                        and getattr(meta, "owner_id", 0)
                        and meta.owner_id != owner_id
                    ):
                        diag_lines.append(
                            f"{d[:8]}=owner_mismatch(meta={meta.owner_id} req={owner_id})"
                        )
                    elif not txt:
                        diag_lines.append(f"{d[:8]}=no_txt")
                    else:
                        diag_lines.append(f"{d[:8]}=txt_present")
                logger.warning(
                    "_build_context returned empty despite doc_ids=%s : %s",
                    session.doc_ids,
                    "; ".join(diag_lines),
                )
            except Exception as _e:
                logger.warning("diagnostic failed: %s", _e)
            # Fall through to the "no documents" marker — the model's
            # default behavior. If the augmented message has extracted
            # metrics, the model will use them.
            prompt_parts.append(marks["no_documents"])
        else:
            prompt_parts.append(marks["no_documents"])

        prompt_parts.append(f"{marks['question_header']}\n{message}")
        prompt_parts.append(marks["instructions"])

        full_prompt = "\n\n".join(prompt_parts)
        session.add("user", message)

        # 3. Query via OpenRouter (GLM-5.1 by default).
        # Final "thinking" line right before the LLM call — replaces any
        # parse-phase status with a generic "Думаю…" so the user sees the
        # transition into actual reasoning.
        yield {"type": "thinking", "code": "thinking", "text": "Думаю…"}
        full_response = ""
        try:
            async for delta in llm.stream(
                prompt=full_prompt,
                system_prompt=system_prompt(),
            ):
                full_response += delta
                yield {"type": "text_delta", "text": delta}
        except Exception as e:
            error_msg = t("chat.agentError", error=e)
            yield {"type": "text_delta", "text": error_msg}
            full_response = error_msg

        session.add("assistant", full_response)

        if sources:
            yield {"type": "sources", "sources": sources}

        yield {"type": "done"}

    def query(
        self,
        message: str,
        session_id: str = "",
        doc_ids: list[str] | None = None,
        owner_id: int | None = None,
    ) -> dict:
        import asyncio
        loop = asyncio.new_event_loop()

        async def _collect():
            text = ""
            sess_id = ""
            sources = []
            async for event in self.query_stream(message, session_id, doc_ids, owner_id=owner_id):
                if event["type"] == "session_id":
                    sess_id = event["session_id"]
                elif event["type"] == "text_delta":
                    text += event["text"]
                elif event["type"] == "sources":
                    sources = event["sources"]
            return text, sess_id, sources

        text, sess_id, sources = loop.run_until_complete(_collect())
        loop.close()
        return {"answer": text, "session_id": sess_id, "sources": sources}


_agent: FinengineAgent | None = None


def get_agent() -> FinengineAgent:
    global _agent
    if _agent is None:
        _agent = FinengineAgent()
    return _agent
