"""`id.entity` — устойчивая идентичность юридического субъекта (A6,
docs/build-plan/11-srez-01-run-passport.md).

Переиспользует существующее определение имени компании
(`bulk_extractor.company_detection`) и добавляет ровно то, чего там нет —
устойчивый `entity_id`. Карточка `id.entity` явно требует: «название само по
себе не считается устойчивым ключом». Поэтому `entity_id` — детерминированная
производная от проверяемых внешних маркеров (ИНН/EIN/...), когда они найдены
в тексте, и только при их отсутствии — от канонического имени, с явным
`confidence_low=True`: два документа с одинаковым отображаемым именем не
считаются доказанно одной сущностью без данных, которые их различают
(настоящее кросс-документное сопоставление — `SubjectProfile`, Срез 11,
будущая сессия; строить мини-реестр юрлиц здесь было бы забеганием вперёд).
"""
from __future__ import annotations

import hashlib
import re

from finengine.contracts import EntityIdentity
from finengine.skills.bulk_extractor.company_detection import (
    _detect_company_from_text,
    _looks_like_auditor_name,
    _looks_like_generic_company_label,
    _prefer_detected_company_name,
)

_GROUP_MARKERS = re.compile(r"\bгрупп[аеы]\b|\bgroup\b|\bхолдинг\w*\b|\bholding\b", re.I)

_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lei", re.compile(r"\bLEI\s*[:#№-]?\s*([A-Z0-9]{18}\d{2})\b", re.I)),
    ("inn", re.compile(r"\b(?:ИНН|INN)\s*[:#№-]?\s*(\d{10}|\d{12})\b", re.I)),
    ("ogrn", re.compile(r"\b(?:ОГРН|OGRN)\s*[:#№-]?\s*(\d{13}|\d{15})\b", re.I)),
    ("ein", re.compile(r"\bEIN\s*[:#№-]?\s*(\d{2}-?\d{7})\b", re.I)),
    ("cik", re.compile(r"\bCIK\s*[:#№-]?\s*(\d{1,10})\b", re.I)),
)


def _external_ids(text: str) -> dict[str, str]:
    """Extract explicitly labelled identifiers, never unlabelled digit runs."""
    result: dict[str, str] = {}
    for kind, pattern in _IDENTIFIER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            result[kind] = re.sub(r"[^A-Z0-9]", "", match.group(1).upper())
    return result


def _entity_id(canonical_name: str, *, external_ids: dict[str, str]) -> str:
    if external_ids:
        key = "verified|" + "|".join(f"{k}:{v}" for k, v in sorted(external_ids.items()))
    else:
        key = "name|" + canonical_name.strip().casefold()
    return "ent-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def identify_entity(
    text: str,
    *,
    filename: str = "",
    existing_name: str = "",
) -> EntityIdentity:
    """Определить каноническое имя, тип субъекта и устойчивый `entity_id`.

    `existing_name` — имя, уже известное из пользовательского/проектного
    ярлыка или старой метакарты; логика выбора между ним и найденным в тексте
    именем не дублируется — берётся существующая `_prefer_detected_company_name`.
    """
    detected = _detect_company_from_text(text) or ""
    canonical = _prefer_detected_company_name(existing_name, detected, filename)
    raw = detected or existing_name
    external_ids = _external_ids(text)

    if not canonical:
        fallback = filename or "unknown"
        return EntityIdentity(
            entity_id=_entity_id(fallback, external_ids=external_ids),
            canonical_name=fallback,
            raw_name=raw or fallback,
            entity_type="issuer",
            external_ids=external_ids,
            confidence_low=True,
            reason="ни текст, ни имя файла не дали различимого названия субъекта",
            source="filename_fallback" if filename else "no_identifier_found",
        )

    # Низкая уверенность: имя похоже на аудитора или на общий ярлык
    # («Банк», «Отчёт») — то есть детектор, вероятно, промахнулся, а не нашёл
    # реальный субъект (см. докстринг `company_detection._looks_like_auditor_name`).
    low_confidence = _looks_like_auditor_name(canonical) or _looks_like_generic_company_label(canonical)
    entity_type = "group" if _GROUP_MARKERS.search(canonical) else "issuer"

    return EntityIdentity(
        entity_id=_entity_id(canonical, external_ids=external_ids),
        canonical_name=canonical,
        raw_name=raw or canonical,
        entity_type=entity_type,
        external_ids=external_ids,
        confidence_low=low_confidence,
        reason=(
            "похоже на аудитора или общий ярлык, а не на субъект отчёта"
            if low_confidence
            else ("определено по тексту документа" if detected else "взято из существующего названия")
        ),
        source="document_text" if detected else "existing_name",
    )
