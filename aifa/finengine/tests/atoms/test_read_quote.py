"""`read.quote` — критерий готовности атома (docs/build-plan/
15b-srez-05b-read-structure-quote-entities.md, карточка `read.quote`):

1. Текст каждой принятой цитаты — точная подстрока нормализованного текста
   указанной страницы в 100% тестов.
2. Неверная страница, изменённая цитата (перефразировка) не получает
   доверенный статус.
3. Цитата хранит документ, версию, страницу и диапазон/якорь для повторной
   проверки.
4. Атом не делает аналитический вывод — только извлекает доказательство.
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import QuoteEvidence
from finengine.skills import two_stage_extractor
from finengine.skills.atoms.read_quote import (
    QuoteResponseInvalid,
    _pages_from_document_text,
    _quote_evidence_for_topic,
    read_quote_batch,
)

_DOCUMENT_TEXT = (
    "===PAGE 1===\n"
    "The Board of Directors recommended not to pay dividends for 2024.\n"
    "===PAGE 2===\n"
    "The effective tax rate was 20% in the reporting period.\n"
    "===PAGE 3===\n"
    "The effective tax rate was 20% in the reporting period.\n"
)


def _patch_call_json_llm(monkeypatch, response):
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    return calls


def test_topic_without_text_is_not_asked(monkeypatch):
    calls = _patch_call_json_llm(monkeypatch, {"quotes": {}})
    result = read_quote_batch([""], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)
    assert result == {"": []}
    assert calls == [], "тема без непустой строки не должна доходить до модели"


def test_one_physical_call_for_the_whole_batch(monkeypatch):
    response = {"quotes": {
        "dividends": {"page": 1, "quote": "The Board of Directors recommended not to pay dividends for 2024."},
        "tax rate": {"page": 2, "quote": "The effective tax rate was 20% in the reporting period."},
    }}
    calls = _patch_call_json_llm(monkeypatch, response)
    result = read_quote_batch(
        ["dividends", "tax rate"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert len(calls) == 1, "read.quote обязан спросить модель ОДИН раз на весь батч тем"
    assert set(result) == {"dividends", "tax rate"}
    assert len(result["dividends"]) == 1 and len(result["tax rate"]) == 1


def test_exact_quote_is_accepted_with_full_provenance(monkeypatch):
    response = {"quotes": {"dividends": {
        "page": 1, "quote": "The Board of Directors recommended not to pay dividends for 2024.",
        "section": "Dividends",
    }}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_quote_batch(["dividends"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)
    evidence = result["dividends"][0]
    assert isinstance(evidence, QuoteEvidence)
    assert evidence.page == 1
    assert evidence.section == "Dividends"
    assert evidence.document_version == "v1"
    assert evidence.fragment_hash
    assert evidence.range_start is not None and evidence.range_end is not None
    assert evidence.range_end > evidence.range_start
    assert evidence.produced_by_atom == "read.quote"


def test_wrong_page_is_rejected_even_if_the_text_exists_elsewhere(monkeypatch):
    """Повторяющийся текст на страницах 2 и 3 — модель заявила страницу 1, где
    этого текста нет вовсе: не получает доверенный статус, даже хотя тот же
    текст реально существует на других страницах документа."""
    response = {"quotes": {"tax rate": {
        "page": 1, "quote": "The effective tax rate was 20% in the reporting period.",
    }}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_quote_batch(["tax rate"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)
    assert result["tax rate"] == []


def test_repeated_text_is_accepted_when_the_claimed_page_is_correct(monkeypatch):
    response = {"quotes": {"tax rate": {
        "page": 3, "quote": "The effective tax rate was 20% in the reporting period.",
    }}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_quote_batch(["tax rate"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)
    assert len(result["tax rate"]) == 1
    assert result["tax rate"][0].page == 3


def test_paraphrase_is_rejected_not_downgraded():
    pages = _pages_from_document_text(_DOCUMENT_TEXT)
    evidence = _quote_evidence_for_topic(
        "dividends", {"page": 1, "quote": "The board decided to skip dividends this year."}, pages,
        document_id="doc-1", document_version="v1",
    )
    assert evidence is None


def test_invalid_model_response_raises_for_the_executor_to_retry(monkeypatch):
    _patch_call_json_llm(monkeypatch, {"not_quotes": {}})
    with pytest.raises(QuoteResponseInvalid):
        read_quote_batch(["dividends"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)


def test_one_topic_missing_from_response_does_not_lose_the_others(monkeypatch):
    response = {"quotes": {
        "dividends": {"page": 1, "quote": "The Board of Directors recommended not to pay dividends for 2024."},
    }}
    _patch_call_json_llm(monkeypatch, response)
    result = read_quote_batch(
        ["dividends", "unknown topic"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert len(result["dividends"]) == 1
    assert result["unknown topic"] == []
