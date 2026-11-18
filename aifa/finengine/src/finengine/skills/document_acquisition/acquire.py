"""Оркестратор A5 — Срез 7: поиск кандидатов → приоритет источника →
безопасное скачивание → регистрация Document.

После регистрации — ничего специфичного для «веба»: `find_and_register_report`
возвращает `doc_id`, дальше который идёт через `doc.probe`/`doc.textify`
(Срез 1/5a) ровно так же, как `doc_id` от ручной загрузки — эквивалентность
проверена отдельно (`tests/integration/test_document_acquisition_equivalence.py`).

Planner integration (26-arhitekturnaya-fiksaciya-v9.md): это НЕ `AtomDef` и не
GoalType-специфичный оркестратор — просто ещё один способ получить `doc_id`
ДО того, как `GoalIR`/`Target` вообще строится, ровно как ручная загрузка
(`/documents/upload`) тоже происходит до `Planner`. `ProducerResolver`/
`GraphPlanner` про эту функцию не знают и не обязаны знать — «After Document
registered downstream producer resolution is unchanged»."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .downloader import DownloadRejected, DownloadResult, download_pdf_candidate
from .policy import SearchCandidate, rank_candidates
from .registration import AcquisitionProvenance, RegistrationResult, register_acquired_document
from .search import search_report_candidates
from .url_safety import UnsafeUrlError

DownloaderFn = Callable[..., Awaitable[DownloadResult]]
RegisterFn = Callable[..., RegistrationResult]
SearcherFn = Callable[..., Awaitable[list[SearchCandidate]]]


@dataclass(frozen=True)
class AcquisitionOutcome:
    status: str  # "registered" | "duplicate" | "unavailable"
    doc_id: str | None = None
    sha256: str | None = None
    source_url: str | None = None
    source_tier: str | None = None
    candidates_tried: int = 0
    reason: str = ""


async def find_and_register_report(
    company: str,
    *,
    owner_id: int,
    period: str = "",
    known_issuer_domains: tuple[str, ...] = (),
    max_candidates: int = 8,
    searcher: SearcherFn | None = None,
    downloader: DownloaderFn | None = None,
    register: RegisterFn | None = None,
) -> AcquisitionOutcome:
    """Найти и зарегистрировать отчётность компании.

    Перебирает кандидатов по убыванию приоритета источника (официальный тир
    сначала, дальше по релевантности поиска), пропуская небезопасные/
    недоступные/не-PDF кандидаты — до первого успеха. Ни один шаг не
    привилегирует "официальный" источник в обход `url_safety`/`downloader` —
    приоритет влияет только на ПОРЯДОК попыток (`policy.py`)."""
    _search = searcher or search_report_candidates
    _download = downloader or download_pdf_candidate
    _register = register or register_acquired_document

    candidates = await _search(company, period=period, max_results=max_candidates)
    if not candidates:
        return AcquisitionOutcome(status="unavailable", reason="поиск не вернул кандидатов")

    ranked = rank_candidates(candidates, known_issuer_domains=known_issuer_domains)
    if not ranked:
        return AcquisitionOutcome(
            status="unavailable", reason="все кандидаты отвергнуты на этапе разбора URL",
        )

    search_query = f"{company} {period}".strip()
    tried = 0
    last_reason = ""
    for ranked_candidate in ranked:
        tried += 1
        url = ranked_candidate.candidate.url
        try:
            result = await _download(url)
        except (UnsafeUrlError, DownloadRejected) as exc:
            last_reason = str(exc)
            continue

        provenance = AcquisitionProvenance(
            source_url=result.final_url,
            source_domain=ranked_candidate.hostname,
            source_tier=ranked_candidate.tier.value,
            search_query=search_query,
            fetched_at=time.time(),
            http_status=result.http_status,
            content_type=result.content_type,
            redirect_chain=result.redirect_chain,
        )
        reg = _register(
            content=result.content, filename_hint=f"{company}.pdf", owner_id=owner_id, provenance=provenance,
        )
        return AcquisitionOutcome(
            status="duplicate" if reg.duplicate else "registered",
            doc_id=reg.doc_id, sha256=reg.sha256, source_url=result.final_url,
            source_tier=ranked_candidate.tier.value, candidates_tried=tried,
        )

    return AcquisitionOutcome(
        status="unavailable",
        reason=last_reason or "ни один кандидат не прошёл проверку/скачивание",
        candidates_tried=tried,
    )


__all__ = ["AcquisitionOutcome", "find_and_register_report"]
