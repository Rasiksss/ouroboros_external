"""`locate` — найти адреса возможных доказательств для концептов, не извлекая
окончательное значение (карточка `locate`, A7, 15a).

Это адаптация РЕЗУЛЬТАТА первого этапа `two_stage_extractor` (сжатие: таблицы
и цитаты на показатель), не второй парсер той же задачи — промпт и модельный
вызов ниже переиспользуются буквально теми же функциями, которыми сегодня
пользуется первый этап (`_build_main_compression_prompt`, `_call_json_llm`,
`_enrich_compression_with_alias_table_matches`). Логическая гранулярность —
на концепт (каждый концепт получает свой список `CandidateLocator`).
`GraphExecutor` отдаёт сюда все концепты документа/периода одним батчем
(`AtomDef.batch_key_fields`), а физически батч режется на шарды по
`settings.locate_batch_size` показателей: шарды идут одновременно под общим
темпом `settings.extract_batch_jobs`, каждый читает весь отчёт. Один вызов на
58 показателей не укладывался в предел ожидания клиента (08.10.2026).
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence

from finengine.config import settings
from finengine.contracts import CandidateLocator, SourceLocator

ATOM_VERSION = "1.0.2"

# Модель не отдаёт калиброванную вероятность на кандидата — это ГРУБАЯ,
# намеренно неточная метка «откуда взялся кандидат», не измеренная точность.
_CONFIDENCE_MODEL_TABLE = 0.6
_CONFIDENCE_ALIAS_TABLE = 0.4
_CONFIDENCE_QUOTE = 0.5
_MAX_TABLE_FRAGMENT_CHARS = 4000


def _bounded_complete_lines(lines: Sequence[str]) -> str:
    kept: list[str] = []
    used = 0
    for line in lines:
        text = str(line)
        added = len(text) + (1 if kept else 0)
        if used + added > _MAX_TABLE_FRAGMENT_CHARS:
            continue
        kept.append(text)
        used += added
    return "\n".join(kept)


def _focused_table_fragment(table, aliases: Sequence[str]) -> str:
    """Keep table headers and the rows that caused this candidate to exist.

    A blind ``[:4000]`` loses metrics near the bottom of long statutory forms:
    ``locate`` still selects the correct table from its full text, but
    ``read.value`` then receives only the beginning of that table.  Work with
    complete OCR lines so a value is never cut in half, and retain the first
    rows because they carry period headers.
    """
    parts = [part for part in (table.context_before, table.text, table.context_after) if part]
    full_text = "\n".join(parts)
    if len(full_text) <= _MAX_TABLE_FRAGMENT_CHARS:
        return full_text

    lines = str(table.text or "").splitlines()
    folded_aliases = [
        str(alias or "").strip().casefold().replace("ё", "е")
        for alias in aliases
        if str(alias or "").strip()
    ]
    hit_indexes = [
        index
        for index, line in enumerate(lines)
        if any(alias in line.casefold().replace("ё", "е") for alias in folded_aliases)
    ]
    if not hit_indexes:
        return _bounded_complete_lines(full_text.splitlines())

    selected = set(range(min(3, len(lines))))
    for index in hit_indexes:
        selected.update(range(max(0, index - 2), min(len(lines), index + 3)))

    focused_parts = []
    if table.context_before:
        focused_parts.extend(str(table.context_before).splitlines())
    focused_parts.extend(lines[index] for index in sorted(selected))
    if table.context_after and any(
        alias in str(table.context_after).casefold().replace("ё", "е")
        for alias in folded_aliases
    ):
        focused_parts.extend(str(table.context_after).splitlines())
    return _bounded_complete_lines(focused_parts)


def _candidates_for_metric(
    enum: str,
    metric: dict,
    compression: dict,
    table_by_id: dict,
    *,
    document_id: str,
    document_version: str,
) -> list[CandidateLocator]:
    from finengine.skills.two_stage_extractor import (
        _compression_payload_for_prompt,
        _expand_related_table_ids,
        _resolve_table_id,
    )

    payload = _compression_payload_for_prompt(compression)
    item = payload.get(enum) or payload.get(str(enum).strip().upper()) or {}
    if not isinstance(item, dict):
        item = {}

    origin_by_id: dict[str, str] = {}
    aliases_by_id: dict[str, list[str]] = {}
    raw_ids: list[str] = []
    for ref in item.get("tables") or []:
        if isinstance(ref, str):
            table_id = _resolve_table_id(ref.strip(), table_by_id)
        elif isinstance(ref, dict):
            table_id = _resolve_table_id(str(ref.get("table_id") or ref.get("id") or ""), table_by_id)
        else:
            continue
        if not table_id:
            continue
        raw_ids.append(table_id)
        is_alias = isinstance(ref, dict) and ref.get("evidence_source") == "alias_expansion"
        origin_by_id[table_id] = "alias_match" if is_alias else "model"
        if isinstance(ref, dict):
            aliases_by_id.setdefault(table_id, []).extend(
                str(alias) for alias in (ref.get("matched_aliases") or []) if str(alias or "").strip()
            )

    metric_aliases = [
        str(value)
        for key in ("name_ru", "name_en", "aliases_ru", "aliases_en")
        for value in ([metric.get(key)] if key.startswith("name_") else (metric.get(key) or []))
        if str(value or "").strip()
    ]

    candidates: list[CandidateLocator] = []
    for table_id in _expand_related_table_ids(raw_ids, table_by_id):
        table = table_by_id[table_id]
        origin = origin_by_id.get(table_id, "model")
        text_fragment = _focused_table_fragment(
            table, aliases_by_id.get(table_id) or metric_aliases,
        )
        candidates.append(CandidateLocator(
            produced_by_atom="locate", produced_by_atom_version=ATOM_VERSION,
            document_id=document_id, document_version=document_version, concept=enum,
            locator=SourceLocator(
                page=table.page, table_or_section=table.title or table_id, text_fragment=text_fragment,
            ),
            confidence=_CONFIDENCE_MODEL_TABLE if origin == "model" else _CONFIDENCE_ALIAS_TABLE,
            origin=origin,
        ))

    quotes = item.get("quotes") or item.get("text_quotes") or []
    if isinstance(quotes, list):
        for quote in quotes[:6]:
            if not isinstance(quote, dict):
                continue
            text = str(quote.get("text") or "").strip()
            if not text:
                continue
            page_raw = quote.get("page")
            try:
                page = int(page_raw) if page_raw is not None else None
            except (TypeError, ValueError):
                page = None
            candidates.append(CandidateLocator(
                produced_by_atom="locate", produced_by_atom_version=ATOM_VERSION,
                document_id=document_id, document_version=document_version, concept=enum,
                locator=SourceLocator(page=page, text_fragment=text),
                confidence=_CONFIDENCE_QUOTE, origin="model",
            ))
    return candidates


def _metric_shards(metrics: list[dict]) -> list[list[dict]]:
    size = max(1, int(settings.locate_batch_size))
    return [metrics[start:start + size] for start in range(0, len(metrics), size)]


async def _locate_shard(
    semaphore: asyncio.Semaphore,
    shard: list[dict],
    *,
    document_text: str,
    tables: list,
    system_prompt: str,
    period_current: str,
    period_prev: str,
    periods_detected: list[str],
    language: str,
    model_id: str | None,
) -> dict:
    from finengine.skills.two_stage_extractor import (
        _build_main_compression_prompt,
        _call_json_llm,
        _enrich_compression_with_alias_table_matches,
    )

    prompt = _build_main_compression_prompt(
        pdf_text_for_prompt=document_text, tables=tables, metrics=shard,
        period_current=period_current, period_prev=period_prev,
        periods_detected=periods_detected, language=language,
    )
    async with semaphore:
        compression, _request, _raw = await _call_json_llm(
            prompt=prompt, system=system_prompt, model_id=model_id, stage="locate",
            debug_dir=None, prompt_name="locate_prompt", response_name="locate_response.json",
        )
    return _enrich_compression_with_alias_table_matches(
        compression, metrics=shard, tables=tables, language=language,
    )


async def _locate_all_shards(shards: list[list[dict]], **shard_kwargs) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, int(settings.extract_batch_jobs)))
    return await asyncio.gather(*(
        _locate_shard(semaphore, shard, **shard_kwargs) for shard in shards
    ))


def locate_batch(
    concepts: Sequence[str],
    *,
    document_id: str,
    document_version: str,
    document_text: str,
    metrics: list[dict],
    company_type: str = "",
    reporting_standard: str = "",
    language: str = "ru",
    period_current: str = "",
    period_prev: str = "",
    periods_detected: list[str] | None = None,
    model_id: str | None = None,
) -> dict[str, list[CandidateLocator]]:
    """Кандидаты для ВСЕХ `concepts`: вызов модели на шард показателей, шарды
    одновременно. Отсутствующий в реестре концепт получает пустой список
    кандидатов молча — `locate` не обязан «найти хоть что-нибудь» (краевой
    случай карточки).
    """
    from finengine.skills.two_stage_extractor import (
        _build_report_tables_for_two_stage,
        _main_financial_metric_system_prompt,
    )

    # Клавиши результата — ОРИГИНАЛЬНЫЕ строки `concepts` (то, чем вызывающий
    # потом делает `result[concept]`), сопоставление с реестром — по
    # верхнему регистру: `NodeArgs.concept` в графе уже приходит как enum
    # реестра (верхний регистр), но чистая функция не обязана на это молча
    # полагаться, чтобы остаться корректной и вызванная напрямую в тестах.
    upper_to_original: dict[str, str] = {}
    for c in concepts:
        upper_to_original.setdefault(str(c or "").strip().upper(), c)
    wanted = set(upper_to_original)
    target_metrics = [m for m in metrics if str(m.get("enum", "")).strip().upper() in wanted]
    result: dict[str, list[CandidateLocator]] = {c: [] for c in concepts}
    if not target_metrics:
        return result

    tables = _build_report_tables_for_two_stage(document_text, None)
    table_by_id = {table.table_id: table for table in tables}
    shards = _metric_shards(target_metrics)
    compressions = asyncio.run(_locate_all_shards(
        shards, document_text=document_text, tables=tables,
        system_prompt=_main_financial_metric_system_prompt(reporting_standard, language),
        period_current=period_current, period_prev=period_prev,
        periods_detected=periods_detected or [], language=language, model_id=model_id,
    ))

    for shard, compression in zip(shards, compressions):
        for metric in shard:
            enum = str(metric["enum"]).strip().upper()
            original_key = upper_to_original.get(enum, enum)
            result[original_key] = _candidates_for_metric(
                enum, metric, compression, table_by_id,
                document_id=document_id, document_version=document_version,
            )
    return result


__all__ = ["ATOM_VERSION", "locate_batch"]
