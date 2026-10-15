"""`read.entities` — отношения субъектов (дочерние/ассоциированные компании,
совместные предприятия, доли участия) как `EntityRelation` evidence вместо
специального транспортного исключения `group` (карточка `read.entities`, A7,
15b).

Адаптер вокруг ТЕКУЩЕГО извлечения, буквально: `read_entities_batch` вызывает
`bulk_extractor.consolidated_group._extract_consolidated_group_full_context`
БЕЗ изменений — тот же промпт, тот же отбор таблиц-кандидатов, та же модельная
граница, что и у боевого пути (`two_stage_extractor.py` вызывает эту же
функцию для `group_event`). Новый код здесь — только преобразование готовых
событий в `EntityRelation`.

🔴 ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ ЭТОЙ СЕССИИ (зафиксировано, не замаскировано).
`_extract_consolidated_group_full_context` отбирает и запрашивает у модели
ТОЛЬКО дочерние общества — его промпт буквально содержит «Do NOT extract
associates... Do NOT extract joint ventures», а отбор таблиц-кандидатов
(`_consolidated_group_candidate_tables`) штрафует таблицы без «дочернего»
контекста. Это осознанное продуктовое решение прежней сессии (см. докстринг
`consolidated_group.py`, #251/#252), не дефект этого атома — но оно означает,
что через РЕАЛЬНЫЙ вызов модели этот атом сегодня физически не может вернуть
`relation="associate"`/`"joint_venture"`: карточка требует не отбрасывать
неизвестный вид отношения молча, а не требует немедленно расширять отбор
кандидатов и промпт настолько чувствительного, не раз чинившегося экстрактора
в этой же сессии. Поэтому `_entity_relations_from_group_event` — отдельная,
проверяемая НАПРЯМУЮ функция: она не отбрасывает НИКАКОЙ `group_relation`
(в отличие от `_group_row_is_non_subsidiary` внутри `_normalise_consolidated_
group_rows`), и `tests/atoms/test_read_entities.py` подтверждает это
конструированием события с `group_relation="associate"`/`"joint_venture"`
напрямую — не дожидаясь, когда экстрактор когда-нибудь научится их находить.
Расширение отбора кандидатов/промпта до реального возврата associate/JV —
отдельная, более крупная задача вне периметра этой сессии.
"""
from __future__ import annotations

import asyncio
import re

from finengine.contracts import EntityRelation
from finengine.skills.bulk_extractor.consolidated_group import (
    CONSOLIDATED_GROUP_ENUM,
    _extract_consolidated_group_full_context,
)

ATOM_VERSION = "1.0.0"

_LEADING_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse_ownership_percent(raw_value: object) -> float | None:
    """Числовая доля из СВОБОДНОГО текста (`"100,00%"`, `"75% минус одна
    акция"`, `"-"`). `"-"`/пусто → `None` явно (решение репозитория «не
    угадывать»), не 0 — отсутствие доли не то же самое, что доля в 0%.
    `"75% минус одна акция"` даёт приближённое `75.0` — исходный текст с
    оговоркой остаётся отдельно в `ownership_percent_raw`, приближение не
    подменяет его."""
    if raw_value is None:
        return None
    match = _LEADING_NUMBER_RE.search(str(raw_value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _entity_relations_from_group_event(
    event: object, *, object_entity: str, document_id: str, document_version: str,
) -> list[EntityRelation]:
    """Одно событие `_normalise_consolidated_group_rows` (или синтетическое
    событие ТОЙ ЖЕ формы в тестах) → одна `EntityRelation` на КАЖДЫЙ период
    (карточка: «два периода» — доля может отличаться по годам, это не два
    независимых факта об одной и той же дате). Не привязан к конкретному
    `group_relation` — неизвестный вид отношения становится `EntityRelation`
    с этим же текстом в `relation`, а не отбрасывается."""
    if not isinstance(event, dict) or event.get("type") != "metric":
        return []
    subject = str(event.get("name_ru") or event.get("segment") or "").strip()
    if not subject:
        return []
    relation = str(event.get("group_relation") or "").strip() or "unknown"
    fallback_page = event.get("page")

    relations: list[EntityRelation] = []
    for pv in event.get("period_values") or []:
        if not isinstance(pv, dict):
            continue
        period = str(pv.get("period") or "").strip() or None
        raw_value = pv.get("value")
        page_raw = pv.get("page") if pv.get("page") is not None else fallback_page
        try:
            page = int(page_raw) if page_raw is not None else None
        except (TypeError, ValueError):
            page = None
        relations.append(EntityRelation(
            produced_by_atom="read.entities", produced_by_atom_version=ATOM_VERSION,
            source_ref=f"{document_id}#{subject}", document_version=document_version,
            concept=CONSOLIDATED_GROUP_ENUM, period_ref=period,
            subject_entity=subject, relation=relation, object_entity=object_entity,
            direction="object_owns_subject", page=page,
            ownership_percent=_parse_ownership_percent(raw_value),
            ownership_percent_raw=(str(raw_value).strip() if raw_value is not None else None),
        ))
    return relations


def read_entities_batch(
    *,
    document_id: str,
    document_version: str,
    document_text: str,
    period_current: str,
    period_prev: str,
    company_display: str,
    reporting_standard: str = "",
    company_type: str = "",
    model_id: str | None = None,
) -> list[EntityRelation]:
    """Один физический вызов модели (та же экономика, что у боевого пути —
    `_extract_consolidated_group_full_context` сама по себе не батчируется
    по показателям, у неё нет измерения «концепт»)."""
    async def _collect() -> list[dict]:
        events: list[dict] = []
        async for event in _extract_consolidated_group_full_context(
            pdf_text_for_prompt=document_text, period_current=period_current, period_prev=period_prev,
            company_display=company_display, reporting_standard=reporting_standard,
            company_type=company_type, model_id=model_id,
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())
    relations: list[EntityRelation] = []
    for event in events:
        try:
            relations.extend(_entity_relations_from_group_event(
                event, object_entity=company_display, document_id=document_id, document_version=document_version,
            ))
        except Exception:  # noqa: BLE001 — сбой разбора ОДНОГО события не должен унести уже собранные отношения (15b, критерий готовности связки extraction, п.5).
            continue
    return relations


__all__ = ["ATOM_VERSION", "read_entities_batch"]
