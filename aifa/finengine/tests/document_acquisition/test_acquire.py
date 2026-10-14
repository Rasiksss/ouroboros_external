"""acquire.py — оркестратор find_and_register_report. Поиск/скачивание/
регистрация подменены двойниками (DI) — проверяется логика перебора
кандидатов, а не сеть/база."""
from __future__ import annotations

import pytest

from finengine.skills.document_acquisition.acquire import find_and_register_report
from finengine.skills.document_acquisition.downloader import DownloadRejected, DownloadResult
from finengine.skills.document_acquisition.policy import SearchCandidate
from finengine.skills.document_acquisition.registration import RegistrationResult
from finengine.skills.document_acquisition.url_safety import UnsafeUrlError


def _result(url: str = "https://www.cbr.ru/report.pdf") -> DownloadResult:
    return DownloadResult(
        content=b"%PDF-1.4 x", final_url=url, content_type="application/pdf",
        http_status=200, redirect_chain=(),
    )


async def test_successful_official_pdf_end_to_end():
    async def searcher(company, *, period, max_results):
        return [SearchCandidate(url="https://www.cbr.ru/report.pdf", search_score=0.5)]

    async def downloader(url, **kwargs):
        return _result(url)

    def register(*, content, filename_hint, owner_id, provenance):
        return RegistrationResult(doc_id="doc-1", sha256="abc123", duplicate=False)

    outcome = await find_and_register_report(
        "Северсталь", owner_id=7, searcher=searcher, downloader=downloader, register=register,
    )
    assert outcome.status == "registered"
    assert outcome.doc_id == "doc-1"
    assert outcome.source_tier == "official_regulator"
    assert outcome.candidates_tried == 1


async def test_falls_through_to_next_candidate_when_first_is_unsafe():
    """Оба кандидата одного (unverified) уровня доверия — ранжирование не
    переставляет их местами, значит порядок попыток задаёт search_score, и
    первый (SSRF) реально пробуется первым и реально отбивается, прежде чем
    оркестратор переходит ко второму."""
    async def searcher(company, *, period, max_results):
        return [
            SearchCandidate(url="http://169.254.169.254/", search_score=0.99),
            SearchCandidate(url="https://random-blog.example.com/report.pdf", search_score=0.1),
        ]

    calls = []

    async def downloader(url, **kwargs):
        calls.append(url)
        if "169.254" in url:
            raise UnsafeUrlError("приватный адрес")
        return _result(url)

    def register(*, content, filename_hint, owner_id, provenance):
        return RegistrationResult(doc_id="doc-2", sha256="def456", duplicate=False)

    outcome = await find_and_register_report(
        "Компания", owner_id=1, searcher=searcher, downloader=downloader, register=register,
    )
    assert outcome.status == "registered"
    assert outcome.candidates_tried == 2
    assert calls == ["http://169.254.169.254/", "https://random-blog.example.com/report.pdf"]


async def test_all_candidates_unavailable():
    async def searcher(company, *, period, max_results):
        return [SearchCandidate(url="https://dead1.example.com/r.pdf"),
                SearchCandidate(url="https://dead2.example.com/r.pdf")]

    async def downloader(url, **kwargs):
        raise DownloadRejected(f"{url}: HTTP 404")

    outcome = await find_and_register_report(
        "Компания", owner_id=1, searcher=searcher, downloader=downloader,
    )
    assert outcome.status == "unavailable"
    assert outcome.candidates_tried == 2
    assert "404" in outcome.reason


async def test_no_search_candidates_at_all():
    async def searcher(company, *, period, max_results):
        return []

    outcome = await find_and_register_report("Никто не найдёт", owner_id=1, searcher=searcher)
    assert outcome.status == "unavailable"
    assert outcome.candidates_tried == 0


async def test_duplicate_existing_document_is_reported_not_re_registered():
    async def searcher(company, *, period, max_results):
        return [SearchCandidate(url="https://www.cbr.ru/report.pdf")]

    async def downloader(url, **kwargs):
        return _result(url)

    def register(*, content, filename_hint, owner_id, provenance):
        return RegistrationResult(doc_id="already-there", sha256="abc123", duplicate=True)

    outcome = await find_and_register_report(
        "Компания", owner_id=1, searcher=searcher, downloader=downloader, register=register,
    )
    assert outcome.status == "duplicate"
    assert outcome.doc_id == "already-there"


async def test_official_source_is_tried_before_unverified_higher_score():
    """Ранжирование `policy.rank_candidates` реально управляет порядком
    попыток оркестратора, не только сортирует список в вакууме."""
    async def searcher(company, *, period, max_results):
        return [
            SearchCandidate(url="https://random-blog.example.com/r.pdf", search_score=0.95),
            SearchCandidate(url="https://www.moex.com/r.pdf", search_score=0.10),
        ]

    calls = []

    async def downloader(url, **kwargs):
        calls.append(url)
        return _result(url)

    def register(*, content, filename_hint, owner_id, provenance):
        return RegistrationResult(doc_id="doc-3", sha256="x", duplicate=False)

    await find_and_register_report(
        "Компания", owner_id=1, searcher=searcher, downloader=downloader, register=register,
    )
    assert calls[0] == "https://www.moex.com/r.pdf"
