"""`read.structure` — многомерная структура (сегменты, сроки погашения,
возраст дебиторки, чувствительность) как типизированный `StructuredFact`
evidence payload вместо специальных транспортных типов на каждую структуру
(карточка `read.structure`, A7, 15b).

Единственный сегодня РЕАЛЬНЫЙ производитель строк — сегментная разбивка:
адаптация РЕЗУЛЬТАТА боевого двухстадийного сегментного пути
(`two_stage_extractor._extract_segment_breakdown_two_stage`, дефолт
`FINENGINE_EXTRACT_ARCH=two_stage`), не более простого прототипа
`bulk_extractor/segments_llm.py` (тот исполняется только в откат-режиме
`full_context` — см. обсуждение архитектурного выбора этой сессии). Ниже
переиспользуются РОВНО те же функции построения промпта
(`_build_segment_compression_prompt`/`_build_segment_extraction_prompt`),
которыми пользуется боевой путь — не второй промпт с тем же смыслом.

🔴 То же намеренное упрощение, что у `read.value` (см. докстринг
`read_value.py`): боевой путь умеет ДОПОЛНИТЕЛЬНО дробить экстракцию на
несколько батчей показателей с ретраями и перемешиванием enum-порядка
(`_batch_items`/`_retry_enum_order`/`asyncio.gather` по батчам). Здесь один
физический батч `GraphExecutor` — это РОВНО два вызова модели (compression +
extraction) на ВЕСЬ запрошенный набор показателей сразу; невалидный ответ
поднимает исключение и уходит в общий retry-бюджет `GraphExecutor`, а не в
собственную логику переспроса с перемешиванием.

`StructureSpec` — не `pydantic.Contract`: как и `MetricDefinition` у
`locate`/`read.value` (15a — там это просто `dict` из `registry_for_profile`),
это внутренний параметр вызова, не тип, путешествующий между процессами.

Сегодня зарегистрирован ровно один `table_kind` — `"segments"`. Сроки
погашения/возраст дебиторки/чувствительность НЕ имеют в репозитории готового
извлекателя (проверено поиском по `maturity`/`aging`/`sensitivit` —
совпадения только в списках исключений «не финансовый показатель», не в
экстракторе) — их producer не пишется в этой сессии (нет базы для
переиспользования, писать с нуля не входит в карточку). Строковый формат
строки (`dimensions`/`concept`/`value`/`unit`/`period_ref`/`page`/`is_total`/
`is_elimination`/`is_open_ended`/`raw_label`) при этом уже сейчас общий для
любой будущей структуры — `tests/atoms/test_read_structure.py` проверяет это
контрактным тестом на срезе погашения, не построенным ни одним боевым
вызовом модели (см. докстринг теста)."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from finengine.contracts import StructuredFact

ATOM_VERSION = "1.0.0"

_KNOWN_DIMENSIONS = ("operating_segment", "product", "geography", "other")

_ELIMINATION_LABEL_KEYWORDS = (
    "elimination", "eliminations", "элиминац", "consolidation adjustment",
    "corporate and eliminations", "corporate & eliminations",
)


class SegmentResponseInvalid(RuntimeError):
    """Ответ модели (compression или extraction) не прошёл валидацию —
    `GraphExecutor` переспрашивает ВЕСЬ физический батч через свой
    `ExecutionBudget.max_retries` (тот же контракт, что `Stage2ResponseInvalid`
    у `read.value`)."""


@dataclass(frozen=True)
class StructureSpec:
    """Один запрошенный вид структуры — `table_kind` + показатели реестра, для
    которых нужна разбивка (для `table_kind="segments"` — уже отфильтрованные
    вызывающим по `bulk._segment_metric_scope`, как и у боевого пути)."""

    table_kind: str
    target_metrics: list[dict] = field(default_factory=list)


def _is_elimination_label(label: str) -> bool:
    low = (label or "").strip().lower()
    return any(keyword in low for keyword in _ELIMINATION_LABEL_KEYWORDS)


def _rows_from_segment_extraction(
    parsed_metrics: dict,
    metric_by_enum: dict[str, dict],
    *,
    period_current: str,
    period_prev: str,
    default_unit: str,
    max_report_year: int | None,
    period_calendar: str,
) -> list[dict]:
    """Разбор ответа модели → плоские строки `StructuredFact.rows`, одна
    строка на (показатель, координата измерения, период) — та же
    гранулярность факта, что у `ScalarFact` (`docstring ScalarFact`: «одно
    число с периодом»), только с добавленными координатами измерения.

    TOTAL-дедупликация — та же, что у боевого пути (`_extract_segment_
    breakdown_two_stage`): нормализованная метка `"TOTAL"`, первое значение
    периода побеждает (`setdefault`), не голосование по сигнатуре — карточка
    просит сохранить `TOTAL`/элиминации как данные, не выбрать «самую частую»
    версию."""
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    rows_by_key: dict[tuple[str, str, str], dict] = {}
    for enum, payload in (parsed_metrics or {}).items():
        enum = str(enum).strip().upper()
        if enum not in metric_by_enum:
            continue
        raw_rows = payload.get("rows") if isinstance(payload, dict) else None
        for raw_row in raw_rows or []:
            if not isinstance(raw_row, dict):
                continue
            raw_label = str(raw_row.get("segment") or "").strip()
            if not raw_label:
                continue
            is_total = bulk._is_total_segment_label(raw_label)
            label = "TOTAL" if is_total else raw_label
            dimension = str(raw_row.get("dimension") or "other").strip().lower() or "other"
            if dimension not in _KNOWN_DIMENSIONS:
                dimension = "other"
            key = (enum, label.casefold(), dimension)
            row = rows_by_key.setdefault(key, {
                "enum": enum, "segment": label, "raw_label": raw_label, "dimension": dimension,
                "values": {}, "unit_text": raw_row.get("unit_text") or "", "page": raw_row.get("page"),
                "is_total": is_total, "is_elimination": _is_elimination_label(raw_label),
            })
            values = raw_row.get("values")
            if isinstance(values, dict):
                for period, value in values.items():
                    row["values"].setdefault(str(period), value)

    flat_rows: list[dict] = []
    for (enum, _label_key, _dim_key), row in rows_by_key.items():
        metric = metric_by_enum.get(enum)
        if metric is None:
            continue
        try:
            period_values, metric_unit = bulk._coerce_period_values(
                row, enum=enum, period_current=period_current, period_prev=period_prev,
                default_unit=default_unit, abs_cost=False, dash_as_zero=True,
                max_report_year=max_report_year, period_calendar=period_calendar,
            )
        except Exception:  # noqa: BLE001 — сбой разбора ОДНОЙ строки не должен унести уже собранные соседние строки батча (15b, критерий готовности связки extraction, п.5).
            continue
        for pv in period_values:
            if pv.get("value") is None:
                continue
            flat_rows.append({
                "dimensions": {row["dimension"]: row["segment"]},
                "concept": enum,
                "value": float(pv["value"]),
                "unit": str(pv.get("unit") or metric_unit or default_unit or "unknown"),
                "period_ref": str(pv.get("period") or ""),
                "page": pv.get("page") if pv.get("page") is not None else row.get("page"),
                "is_total": row["is_total"],
                "is_elimination": row["is_elimination"],
                "is_open_ended": False,
                "raw_label": row["raw_label"],
            })
    return flat_rows


def read_structure_batch(
    specs: Sequence[StructureSpec],
    *,
    document_id: str,
    document_version: str,
    document_text: str,
    period_current: str,
    period_prev: str,
    company_type: str = "",
    reporting_standard: str = "",
    language: str = "ru",
    segment_mode: str = "extended",
    period_calendar: str = "calendar",
    max_report_year: int | None = None,
    prompt_profile_id: str | None = None,
    model_id: str | None = None,
) -> dict[str, list[StructuredFact]]:
    """Один физический вызов compression + один физический вызов extraction
    на весь запрошенный набор `specs` того же `table_kind`, как и у
    `locate_batch`/`read_value_batch`. `table_kind` без единого показателя
    (`target_metrics` пуст — вызывающий уже отфильтровал по ячейке) не
    отправляется модели вовсе."""
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415
    from finengine.skills.two_stage_extractor import (
        _build_report_tables_for_two_stage,
        _build_segment_compression_prompt,
        _build_segment_extraction_prompt,
        _build_finengine2_system_prompt,
        _call_json_llm,
        _recover_segment_stage2_json_object,
        prompt_profile_for_cell,
    )

    result: dict[str, list[StructuredFact]] = {spec.table_kind: [] for spec in specs}
    segment_specs = [s for s in specs if s.table_kind == "segments" and s.target_metrics]
    if not segment_specs:
        return result

    target_metrics: list[dict] = []
    seen_enums: set[str] = set()
    for spec in segment_specs:
        for metric in spec.target_metrics:
            enum = str(metric.get("enum", "")).strip().upper()
            if enum and enum not in seen_enums:
                seen_enums.add(enum)
                target_metrics.append(metric)
    if not target_metrics:
        return result

    tables = _build_report_tables_for_two_stage(document_text, None)
    table_by_id = {table.table_id: table for table in tables}
    profile = prompt_profile_for_cell(prompt_profile_id)
    system_prompt = _build_finengine2_system_prompt(profile)

    compress_prompt = _build_segment_compression_prompt(
        pdf_text_for_prompt=document_text, tables=tables, metrics=target_metrics,
        prompt_profile_id=prompt_profile_id, language=profile.language,
    )
    compression, _compression_request, _raw_compression = asyncio.run(_call_json_llm(
        prompt=compress_prompt, system=system_prompt, model_id=model_id, stage="read_structure_compression",
        debug_dir=None, prompt_name="read_structure_compression_prompt",
        response_name="read_structure_compression_response.json",
    ))

    extraction_prompt = _build_segment_extraction_prompt(
        target_metrics=target_metrics, compression=compression, table_by_id=table_by_id,
        segment_mode=segment_mode, company_type=company_type, prompt_profile_id=prompt_profile_id,
    )
    target_enums = [str(m["enum"]).strip().upper() for m in target_metrics]
    _parsed_unused, _extraction_request, raw_extraction = asyncio.run(_call_json_llm(
        prompt=extraction_prompt, system=system_prompt, model_id=model_id, stage="read_structure_extraction",
        debug_dir=None, prompt_name="read_structure_extraction_prompt",
        response_name="read_structure_extraction_response.json",
    ))
    parsed, error, _recovered = _recover_segment_stage2_json_object(raw_extraction, target_enums)
    if error:
        raise SegmentResponseInvalid(f"read.structure: ответ модели не прошёл валидацию: {error}")

    default_unit = bulk._default_unit_for_standard(reporting_standard)
    metric_by_enum = {str(m["enum"]).strip().upper(): m for m in target_metrics}
    # `_recover_segment_stage2_json_object` уже разворачивает верхний ключ
    # `"metrics"` — `parsed` здесь ПЛОСКИЙ `{enum: {"rows": [...]}}`
    # (`_validate_segment_stage2_json_object`), не `{"metrics": {...}}`.
    flat_rows = _rows_from_segment_extraction(
        parsed, metric_by_enum, period_current=period_current, period_prev=period_prev,
        default_unit=default_unit, max_report_year=max_report_year, period_calendar=period_calendar,
    )
    if not flat_rows:
        return result

    fact = StructuredFact(
        produced_by_atom="read.structure", produced_by_atom_version=ATOM_VERSION,
        source_ref=f"segments@{document_id}", document_version=document_version,
        table_kind="segments", rows=flat_rows,
    )
    result["segments"] = [fact]
    return result


__all__ = ["ATOM_VERSION", "SegmentResponseInvalid", "StructureSpec", "read_structure_batch"]
