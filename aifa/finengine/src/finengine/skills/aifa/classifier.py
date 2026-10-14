"""AIFA report-type guard.

AIFA's reranker now ships in two flavours:
- non_financial (RU IFRS) → `evolute_features_automl.cbm`
- bank (RU IFRS)         → `Basic_Banks.cbm`

So the classifier no longer rejects banks — only foreign-language reports
and US-GAAP fall through to the "AIFA не умеет" stub. RU+IFRS bank reports
go on to the bank reranker via company_type='bank' routing in extractors.
"""
from __future__ import annotations

import re


_DEFAULT_MESSAGE = (
    "AIFA ещё не умеет работать с такой отчётностью. "
    "Пожалуйста, выберите другой режим."
)


class AIFAUnsupportedError(Exception):
    """Raised when AIFA is asked to process a document type it can't handle.

    The message is intentionally generic — we don't expose the internal
    reasoning (bank markers, language detection, etc.) to end users.
    """

    def __init__(self, reason: str | None = None):
        self.reason = _DEFAULT_MESSAGE
        super().__init__(self.reason)


def _cyrillic_ratio(text: str) -> float:
    """Fraction of letters that are Cyrillic. Pure-Latin reports score 0."""
    head = text[:30000]
    cyr = sum(1 for c in head if "а" <= c.lower() <= "я" or c in "ёЁ")
    lat = sum(1 for c in head if "a" <= c.lower() <= "z")
    total = cyr + lat
    return cyr / total if total else 0.0


_BANK_MARKERS = (
    "норматив достаточности капитала",
    "капитал первого уровня",
    "tier 1 capital",
    "кредиты клиентам",
    "loans to customers",
    "ecl allowance",
    "резерв под ожидаемые кредитные убытки",
    "процентные доходы и расходы",
    "комиссионные доходы",
    "interbank",
    "межбанковские",
    "процентный доход",
    "процентный расход",
    "обязательные резервы",
    "норматив н1",
    "rwa",
    "risk-weighted assets",
)


def detect_company_kind(pdf_text: str) -> str:
    """Return "bank" when the report reads like a bank IFRS, else "non_financial".

    The user is not asked to mark this manually — extractors call this on the
    OCR text and route the metric registry / reranker accordingly.
    Threshold of >=2 markers in the first 30k chars is the same heuristic that
    used to *reject* bank reports earlier; now it just routes them.
    """
    if not pdf_text:
        return "non_financial"
    head = pdf_text[:30000].lower()
    hits = sum(1 for m in _BANK_MARKERS if m in head)
    return "bank" if hits >= 2 else "non_financial"


def classify_supported(pdf_text: str) -> None:
    """Raise AIFAUnsupportedError when neither reranker can help.

    Now allows banks through (handled by the bank reranker downstream). Still
    rejects: empty/very short text, non-Russian language, US-GAAP reports.
    """
    if not pdf_text or len(pdf_text) < 1000:
        raise AIFAUnsupportedError()

    if _cyrillic_ratio(pdf_text) < 0.30:
        raise AIFAUnsupportedError()

    # Foreign reporting standard hints (US-GAAP, etc.). The bank reranker is
    # IFRS-tuned; we don't ship a US-GAAP variant in prod yet (binary parked
    # under `_v2_Basic_2.cbm` but pipeline not adapted).
    if re.search(r"\bU\.?S\.?\s*GAAP\b", pdf_text[:20000]):
        raise AIFAUnsupportedError()
