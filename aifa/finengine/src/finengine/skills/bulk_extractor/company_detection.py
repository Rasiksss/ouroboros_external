"""Кто это за компания и какого она типа — по тексту отчёта.

⚠️ `_looks_like_auditor_name` живёт здесь не зря: на Ак Барсе в журнал уехало
имя аудитора вместо эмитента (#256), потому что заключение аудитора идёт
первым разделом промежуточного отчёта. Разбирать дефект надо начиная отсюда.

`_detect_year_from_text` из этого набора переехал в period_selection — он
определяет период, а не компанию, и его зовёт выбор периодов прогона.
"""
from __future__ import annotations

from pathlib import Path
from finengine.skills.metric_registry import EXTRACTABLE_BANK, EXTRACTABLE_NON_FINANCIAL


def _has_latin(text: str | None) -> bool:
    import re as _re
    return bool(_re.search(r"[A-Za-z]", text or ""))

def _has_cyrillic(text: str | None) -> bool:
    import re as _re
    return bool(_re.search(r"[А-Яа-яЁё]", text or ""))

def _looks_like_auditor_name(name: str | None) -> bool:
    normalized = (name or "").lower()
    return any(
        marker in normalized
        for marker in (
            "аудит",
            "аудитор",
            "audit",
            "tsatr",
            "цатр",
        )
    )

def _looks_like_generic_company_label(name: str | None) -> bool:
    import re as _re

    normalized = _re.sub(r"[^a-zа-яё0-9]+", " ", (name or "").lower()).strip()
    return normalized in {
        "bank",
        "банк",
        "report",
        "отчет",
        "отчёт",
        "financial statements",
        "финансовая отчетность",
        "финансовая отчётность",
    }

def _looks_like_filename_company_label(name: str | None, filename: str | None) -> bool:
    import re as _re

    if not name or not filename:
        return False
    cleaned_name = _re.sub(r"[^a-zа-яё0-9]+", " ", name.lower()).strip()
    stem = _re.sub(r"\.[a-z]{2,5}$", "", filename.lower(), flags=_re.I)
    cleaned_stem = _re.sub(r"[^a-zа-яё0-9]+", " ", stem).strip()
    if not cleaned_name or not cleaned_stem:
        return False
    return cleaned_name == cleaned_stem or cleaned_name in cleaned_stem or cleaned_stem in cleaned_name

def _company_name_from_filename(filename: str) -> str | None:
    """Pick the most likely brand name from a filename.

    Prefers all-uppercase Cyrillic acronyms (ЧТПЗ, ТМК, ВТБ, ОТП, СБЕР) — they
    are nearly always the issuer brand. Falls back to the longest capitalized
    Cyrillic word (e.g. "Сбербанк", "Кузнецкий"). Returns None if nothing
    looks like a brand.
    """
    import re as _re
    base = _re.sub(r"\.[a-z]{2,5}$", "", filename, flags=_re.I)
    tokens = _re.split(r"[_\-\s.]+", base)
    # 1) all-uppercase Cyrillic 3+ chars — acronyms
    acronyms = [t for t in tokens if _re.fullmatch(r"[А-ЯЁ]{3,8}", t or "")]
    if acronyms:
        # Longest — usually the most specific
        acronyms.sort(key=len, reverse=True)
        return acronyms[0]
    # 2) capitalized Cyrillic 4+ chars (Сбербанк, Лукойл) — exclude generic words
    _GENERIC = {"Отработано", "Отчёт", "Отчет", "Годовая", "Годовой",
                "Финансовая", "Бухгалтерский", "Консолидированная"}
    capitalized = [
        t for t in tokens
        if _re.fullmatch(r"[А-ЯЁ][а-яё]{3,15}", t or "") and t not in _GENERIC
    ]
    if capitalized:
        capitalized.sort(key=len, reverse=True)
        return capitalized[0]
    return None

def _reconcile_company_name(model_name: str, filename: str) -> str:
    """Sanity-check a model-extracted company name against the filename.

    Catches the subsidiary-vs-parent confusion: when a subsidiary's annual
    report says "входит в Группу ТМК" on every page, the LLM grabs "ТМК"
    even though the filename reads "ЧТПЗ 2024 годовая.pdf". This helper
    overrides the model's pick with the filename's acronym when the two
    are clearly disjoint inside the same script.

    English SEC issuer names must stay in their original spelling. A Cyrillic
    filename token such as "АМД" is only a user/file label and must not replace
    "ADVANCED MICRO DEVICES, INC." from the report cover.

    Logic:
      - Extract a brand-like token from the filename.
      - If the filename token is NOT a substring (case-insensitive) of the
        model name AND the model name is NOT a substring of the filename →
        the two names refer to different entities. Trust the filename.
      - Otherwise keep the model name (it's usually the more readable form).
    """
    if not model_name or not filename:
        return model_name or _company_name_from_filename(filename) or ""
    fname_brand = _company_name_from_filename(filename)
    if not fname_brand:
        return model_name
    if _has_latin(model_name) and _has_cyrillic(fname_brand) and not _has_cyrillic(model_name):
        return model_name
    a = model_name.lower()
    b = fname_brand.lower()
    base = filename.lower()
    if b in a or a in base:
        return model_name
    return fname_brand

def _prefer_detected_company_name(existing_name: str | None, detected_name: str | None, filename: str) -> str:
    """Choose a display company name without transliterating report names.

    `existing_name` can come from a user/project label or from older metadata.
    If the document detector finds an original Latin issuer name, prefer it over
    a Cyrillic short label such as "АМД"; then run the usual filename sanity
    check for same-script subsidiary/parent mistakes.
    """
    existing = (existing_name or "").strip()
    detected = (detected_name or "").strip()
    if not detected:
        return _reconcile_company_name(existing, filename)
    if not existing:
        return _reconcile_company_name(detected, filename)
    if (
        _looks_like_auditor_name(existing)
        or _looks_like_generic_company_label(existing)
        or _looks_like_filename_company_label(existing, filename)
    ):
        return _reconcile_company_name(detected, filename)
    if _has_latin(detected) and _has_cyrillic(existing) and not _has_cyrillic(detected):
        return _reconcile_company_name(detected, filename)
    return _reconcile_company_name(existing, filename)

def _detect_company_from_text(text: str) -> str | None:
    """Pull the issuer name out of the document head.

    Tries the long Russian legal-form forms first ("Публичного акционерного
    общества «X»"), then the short abbreviations (ПАО / АО / ОАО / ООО), then
    "Группа/Группе/Группой X" without quotes. Also handles SEC 10-K/10-Q covers
    where the issuer name appears immediately before "Exact name of registrant".
    Returns the inner name without the legal-form prefix.
    """
    import re as _re
    head = text[:20000]
    sec_patterns = [
        r"Commission File Number[^\n]{0,180}I\.R\.S\. Employer Identification No\.\s*\n\s*[0-9-]+\s+([A-Z0-9][A-Z0-9&.,'’\- ]{2,120}?)\s+\d{2}-\d{7}\b",
        r"\n\s*([A-Z][A-Z0-9&.,'’\- ]{3,120}?)\s*\n\s*\(Exact name of registrant as specified in its charter\)",
        r"\n\s*([A-Z][A-Z0-9&.,'’\- ]{3,120}?)\s*\n\s*Exact name of registrant",
    ]
    sec_stopwords = {
        "UNITED STATES",
        "SECURITIES AND EXCHANGE COMMISSION",
        "FORM 10-K",
        "FORM 10-Q",
        "WASHINGTON, D.C",
    }
    for pat in sec_patterns:
        m = _re.search(pat, head, flags=_re.I)
        if not m:
            continue
        name = " ".join(m.group(1).strip().split())
        if 3 <= len(name) <= 120 and name.upper() not in sec_stopwords:
            return name
    bank_stopwords = {
        "РОССИИ",
        "РОССИЙСКОЙ ФЕДЕРАЦИИ",
        "КЛИЕНТОВ",
        "БАНКОВ",
    }
    for raw_line in head.splitlines()[:120]:
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        bank_line = _re.fullmatch(
            r"(?:БАНК|Банк)\s+([А-ЯЁA-Z0-9][А-ЯЁA-Zа-яёa-z0-9 .\-]{1,60})",
            line,
        )
        if not bank_line:
            bank_line = _re.fullmatch(
                r"(?:ПАО|АО|ОАО)\s+(?:БАНК|Банк)\s+«?([^»\"]{2,60})»?",
                line,
            )
        if not bank_line:
            continue
        name = " ".join(bank_line.group(1).strip(" «»\"").split())
        if 2 <= len(name) <= 60 and name.upper() not in bank_stopwords:
            return name
    # Char class includes `.` so abbreviations with dots survive — e.g.
    # «ВЭБ.РФ», «ИК.РФ». Without it the Корпорация/Группа patterns
    # truncated state-corp names mid-token.
    patterns = [
        r"[Пп]убличн\w+\s+акционерн\w+\s+обществ\w+\s+«([^»]{2,80})»",
        r"[Аа]кционерн\w+\s+обществ\w+\s+«([^»]{2,80})»",
        r"[Гг]осударствен\w+\s+корпораци\w+(?:\s+\w+){0,2}\s+«([^»]{2,80})»",
        r"П\s*А\s*О\s+«НК\s+«([^»]{2,40})»",
        r"П\s*А\s*О\s+«([^»]{2,80})»",
        r"О\s*А\s*О\s+«([^»]{2,80})»",
        r"А\s*О\s+«([^»]{2,80})»",
        r"О\s*О\s*О\s+«([^»]{2,80})»",
        r"Групп\w+\s+«([^»]{2,80})»",
        r"Групп\w+\s+([А-ЯA-Z][А-ЯA-Zа-яa-z\-\.]{2,40})",
        r"Корпораци\w+\s+«([^»]{2,80})»",
        r"Корпораци\w+\s+([А-ЯA-Z][А-ЯA-Zа-яa-z\-\.]{2,40})",
        r"[Кк]омпани\w+\s+«([^»]{2,60})»",
    ]
    for pat in patterns:
        m = _re.search(pat, head, flags=_re.I)
        if m:
            name = " ".join(m.group(1).strip().split())
            name = _re.sub(r"^(ПАО|АО|ОАО|ООО)\s+", "", name, flags=_re.I)
            if 2 <= len(name) <= 80:
                return name
    return None

def _registry_phrases_for_segment(segment: str) -> list[str]:
    """Lower-cased phrases that, when found in PDF text, signal that the
    document belongs to the given segment (bank / non_financial).

    We use enums that exist in ONE registry but not the other — those are
    "segment-specific" markers (e.g. LOANS_STAGE3_GROSS exists only in
    EXTRACTABLE_BANK, INVENTORIES only in EXTRACTABLE_NON_FINANCIAL). Each
    metric contributes its Russian/English name plus up to 5 curated aliases.
    """
    nf_enums = {m["enum"] for m in EXTRACTABLE_NON_FINANCIAL}
    bank_enums = {m["enum"] for m in EXTRACTABLE_BANK}
    if segment == "bank":
        target_metrics = [m for m in EXTRACTABLE_BANK if m["enum"] not in nf_enums]
    else:
        target_metrics = [m for m in EXTRACTABLE_NON_FINANCIAL if m["enum"] not in bank_enums]
    phrases: list[str] = []
    for m in target_metrics:
        for key in (m.get("name_ru"), m.get("name_en")):
            if key and len(key) >= 6:
                phrases.append(key.strip().lower())
        for alias in (m.get("aliases_ru") or [])[:5]:
            if alias and alias != "Нет" and len(alias) >= 6:
                phrases.append(alias.strip().lower())
    # Dedupe and drop too-generic 1-word entries
    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        if p in seen or " " not in p:
            continue
        seen.add(p)
        out.append(p)
    return out

_BANK_DETECTION_PHRASES = _registry_phrases_for_segment("bank")

_NF_DETECTION_PHRASES = _registry_phrases_for_segment("non_financial")

def _detect_company_type(text: str) -> str:
    """Pick 'bank' or 'non_financial' from registry-derived markers.

    Uses the segment-specific phrases assembled from the metric registry.
    Counts distinct phrase hits across the WHOLE document. Returns 'bank'
    only when bank markers clearly outnumber NF markers AND there are at
    least 3 of them — the registries share 35 metrics, single-word noise
    isn't enough to flip a non-financial PDF.
    """
    low = text.lower()
    bank_hits = sum(1 for p in _BANK_DETECTION_PHRASES if p in low)
    nf_hits = sum(1 for p in _NF_DETECTION_PHRASES if p in low)
    if bank_hits >= 3 and bank_hits > nf_hits:
        return "bank"
    return "non_financial"

async def _wait_for_pdf_text(
    txt_path: Path,
    timeout_s: int = 900,
    poll_s: float = 1.5,
) -> bool:
    """Poll for the .txt sidecar to appear, yielding to the event loop.

    The /upload endpoint kicks off `_parse_doc_in_background` as a
    BackgroundTask and returns immediately, so /extract can fire before
    parsing has finished writing the sidecar. Instead of erroring out
    right away (the old behavior — "Документ ещё обрабатывается, нажмите
    «Извлечь» снова"), wait up to `timeout_s` seconds for the file to
    show up. Most PDFs parse in 5-30s; long bank reports (100+ pages via
    IDP-OCR @ ~4s/page) need 7-12 min — hence the 15-min cap.
    """
    import asyncio
    import time as _t
    deadline = _t.time() + timeout_s
    while not txt_path.exists():
        if _t.time() > deadline:
            return False
        await asyncio.sleep(poll_s)
    return True
