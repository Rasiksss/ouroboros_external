"""`read.quote` — точная цитата с проверенным адресом источника (карточка
`read.quote`, A7, 15b).

Нет существующего кода-прототипа для переиспользования (в отличие от
`read.structure`/`read.entities`) — карточка сама отмечает «есть разрозненные
фрагменты цитат/страниц в текущем извлечении, но нет общего контракта».
Промпт ниже — новый, минимальный: одна модельная граница на весь батч тем
(та же пакетная экономика, что у `locate`/`read.value`).

Инженерный принцип карточки — «смысл выбирает модель, существование цитаты
проверяет код»: модель предлагает страницу и текст, а
`_quote_evidence_for_topic` детерминированно проверяет, что нормализованный
текст цитаты — точная подстрока нормализованного текста ИМЕННО заявленной
страницы. Не совпало (неверная страница, перефразировка, цитата из другой
версии документа) — `QuoteEvidence` не создаётся вовсе, тема остаётся без
доказательства, а не с недостоверным приближением."""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence

from finengine.contracts import QuoteEvidence

ATOM_VERSION = "1.0.0"

_PAGE_MARKER_RE = re.compile(r"===PAGE\s+(\d+)===\n?")


class QuoteResponseInvalid(RuntimeError):
    """Ответ модели не является объектом с полем `quotes` — `GraphExecutor`
    переспрашивает ВЕСЬ физический батч через свой `ExecutionBudget.max_retries`
    (тот же контракт, что `Stage2ResponseInvalid` у `read.value`)."""


def _pages_from_document_text(document_text: str) -> dict[int, str]:
    """Разбить текст документа на страницы по маркеру `===PAGE N===` — той же
    конвенции, которой уже пользуется `_build_report_tables_for_two_stage`
    (`two_stage_extractor.py`). Документ без маркеров — одна страница №1, тот
    же дефолт, что у `_ingest_document` (`graph_atoms.py`)."""
    text = document_text or ""
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return {1: text}
    pages: dict[int, str] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        try:
            page_no = int(match.group(1))
        except ValueError:
            continue
        pages[page_no] = text[start:end]
    return pages


def _normalize_quote_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def _quote_evidence_for_topic(
    topic: str, raw_item: object, pages: dict[int, str], *, document_id: str, document_version: str,
) -> QuoteEvidence | None:
    if not isinstance(raw_item, dict):
        return None
    quote_text = str(raw_item.get("quote") or "").strip()
    try:
        page = int(raw_item.get("page"))
    except (TypeError, ValueError):
        return None
    quote_norm = _normalize_quote_text(quote_text)
    if not quote_text or not quote_norm or page not in pages:
        return None
    page_text_norm = _normalize_quote_text(pages[page])
    offset = page_text_norm.find(quote_norm)
    if offset < 0:
        # Не найдено буквально на заявленной странице: неверная страница,
        # перефразировка или цитата из другой версии документа — карточка
        # прямо запрещает таким текстам получать статус QuoteEvidence.
        return None
    section = str(raw_item.get("section") or "").strip() or None
    fragment_hash = hashlib.sha256(quote_norm.encode("utf-8")).hexdigest()
    return QuoteEvidence(
        produced_by_atom="read.quote", produced_by_atom_version=ATOM_VERSION,
        source_ref=f"{document_id}@page:{page}", document_version=document_version,
        concept=topic, quote_text=quote_text, page=page, section=section,
        range_start=offset, range_end=offset + len(quote_norm), fragment_hash=fragment_hash,
    )


def read_quote_batch(
    topics: Sequence[str],
    *,
    document_id: str,
    document_version: str,
    document_text: str,
    language: str = "ru",
    model_id: str | None = None,
) -> dict[str, list[QuoteEvidence]]:
    """Один физический вызов модели для ВСЕХ `topics` сразу. Тема без
    непустой строки молча получает пустой результат — как и `locate`, атом
    не обязан «найти хоть что-нибудь»."""
    from finengine.skills.two_stage_extractor import _call_json_llm  # noqa: PLC0415

    result: dict[str, list[QuoteEvidence]] = {t: [] for t in topics}
    wanted = [t for t in topics if str(t or "").strip()]
    if not wanted:
        return result

    pages = _pages_from_document_text(document_text)
    system_prompt = (
        "You find exact literal quotes in financial report text that address the given topics. "
        "Return the quote text EXACTLY as written in the source (same words, same order, same "
        "punctuation), together with the page it appears on. Never paraphrase or summarize. If no "
        "sentence in the text literally addresses a topic, omit that topic from the response."
    )
    topics_block = "\n".join(f"- {topic}" for topic in wanted)
    pages_block = "\n\n".join(f"===PAGE {page}===\n{text}" for page, text in sorted(pages.items()))
    user_prompt = (
        f"# Topics\n{topics_block}\n\n"
        f"# Document pages\n{pages_block}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        '{"quotes": {"<topic exactly as listed above>": '
        '{"page": 1, "quote": "exact sentence from that page", "section": "optional heading"}}}\n'
    )
    parsed, _request, _raw = asyncio.run(_call_json_llm(
        prompt=user_prompt, system=system_prompt, model_id=model_id, stage="read_quote",
        debug_dir=None, prompt_name="read_quote_prompt", response_name="read_quote_response.json",
    ))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("quotes"), dict):
        raise QuoteResponseInvalid("read.quote: ответ модели не содержит объект 'quotes'")

    for topic in wanted:
        try:
            evidence = _quote_evidence_for_topic(
                topic, parsed["quotes"].get(topic), pages, document_id=document_id, document_version=document_version,
            )
        except Exception:  # noqa: BLE001 — сбой разбора ОДНОЙ темы не должен унести уже собранные соседние (15b, критерий готовности связки extraction, п.5).
            continue
        if evidence is not None:
            result[topic] = [evidence]
    return result


__all__ = ["ATOM_VERSION", "QuoteResponseInvalid", "read_quote_batch"]
