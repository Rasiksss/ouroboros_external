"""`read.value` — превратить найденное свидетельство (`CandidateLocator`) в
числовой финансовый факт с полным происхождением (карточка `read.value`, A7,
15a).

Адаптация РЕЗУЛЬТАТА второго этапа `two_stage_extractor` (извлечение значений
из уже сжатого контекста): промпт собирается той же функцией
(`_build_main_extraction_prompt`), которой сегодня пользуется второй этап —
только evidence для неё строится из `CandidateLocator`, а не из внутреннего
словаря `compression`. Детерминированный разбор представления числа
(десятичная запятая, разделители тысяч, скобки, проценты, на акцию, валюты) —
`bulk_extractor.period_values._coerce_period_values`, тот же самый парсер, что
и у сегодняшнего рабочего пути; здесь он не переизобретается.

Один логический batch `GraphExecutor` может содержать весь ролевой реестр
формы. Физически отправлять эти десятки показателей одним JSON-запросом нельзя:
модель обрезает объект, строгая проверка видит `missing_enums`, а повтор общего
батча воспроизводит ту же ошибку. Поэтому вторая стадия сохраняет проверенную
механику bulk extractor: шарды `settings.extract_batch_size`, общий семафор
`settings.extract_batch_jobs`, локальные попытки `extract_batch_attempts`.
Логически это по-прежнему один атом `read.value`, следующий после `locate`;
шардирование — только физическая экономика модельной границы.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence

from finengine.contracts import CandidateLocator, ScalarFact
from finengine.config import settings

ATOM_VERSION = "1.0.0"


class Stage2ResponseInvalid(RuntimeError):
    """Ответ модели не прошёл валидацию по ожидаемому набору enum'ов —
    `GraphExecutor` переспрашивает ВЕСЬ физический батч через свой
    `ExecutionBudget.max_retries`, не эта функция сама по себе."""


def _metric_shards(metrics: list[dict]) -> list[list[dict]]:
    """Bound one model response while retaining the registry order.

    Ad-hoc metrics are singletons deliberately. Their user-defined labels can
    overlap heavily (for example, two adjacent ``Прочие ... активы`` rows),
    and unlike registry entries they have no curated code/alias set to stop a
    valid value from a neighbouring concept being copied across the batch.
    Registered metrics retain the configured batch economy.
    """
    size = max(1, int(settings.extract_batch_size))
    shards: list[list[dict]] = []
    pending: list[dict] = []

    def _flush_pending() -> None:
        while pending:
            shards.append(pending[:size])
            del pending[:size]

    for metric in metrics:
        if metric.get("ad_hoc") is True:
            _flush_pending()
            shards.append([metric])
        else:
            pending.append(metric)
            if len(pending) >= size:
                _flush_pending()
    _flush_pending()
    return shards


def _synthetic_evidence(candidates_by_concept: dict[str, list[CandidateLocator]]):
    """Вид `(compression, table_by_id)`, который ожидает
    `_build_main_extraction_prompt` — но собранный ИЗ `CandidateLocator`, а
    не из живого словаря `compression` первого этапа. Таблица-кандидат
    (`locator.table_or_section` задан) становится синтетическим `ReportTable`
    с телом = `text_fragment`; цитата-кандидат (только `text_fragment`)
    остаётся цитатой."""
    from finengine.skills.two_stage_extractor import ReportTable

    table_by_id: dict[str, ReportTable] = {}
    compression_metrics: dict[str, dict] = {}
    for concept, candidates in candidates_by_concept.items():
        enum = str(concept).strip().upper()
        table_refs: list[dict] = []
        quotes: list[dict] = []
        for idx, candidate in enumerate(candidates):
            locator = candidate.locator
            if locator.table_or_section:
                table_id = f"{enum}-T{idx:03d}"
                table_by_id[table_id] = ReportTable(
                    table_id=table_id, page=locator.page,
                    title=locator.table_or_section, text=locator.text_fragment,
                )
                table_refs.append({"table_id": table_id})
            elif locator.text_fragment:
                quotes.append({"page": locator.page, "text": locator.text_fragment})
        compression_metrics[enum] = {"tables": table_refs, "quotes": quotes}
    return {"metrics": compression_metrics}, table_by_id


def _contains_extracted_value(value: object) -> bool:
    """Whether a validated stage-2 payload contains at least one value.

    Schema-valid JSON may still be an empty extraction (all ``value`` fields
    are null).  For an isolated ad-hoc row that is often a transient model
    miss rather than evidence that the row is absent, so the caller gets to
    spend its existing local retry budget without inventing a value.
    """
    if isinstance(value, dict):
        if "value" in value and value["value"] is not None:
            return True
        values = value.get("values")
        if isinstance(values, dict) and any(
            item is not None and str(item).strip() not in {"", "-", "—"}
            for item in values.values()
        ):
            return True
        return any(_contains_extracted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_extracted_value(item) for item in value)
    return False


def _scalar_facts_for_concept(
    enum: str,
    raw_item: dict,
    candidates: list[CandidateLocator],
    *,
    document_version: str,
    period_current: str,
    period_prev: str,
    default_unit: str,
    period_calendar: str,
    max_report_year: int | None,
) -> list[ScalarFact]:
    from finengine.skills import bulk_extractor as bulk

    fallback_page = next((c.locator.page for c in candidates if c.locator.page), None)
    period_values, _metric_unit = bulk._coerce_period_values(
        raw_item, enum=enum, period_current=period_current, period_prev=period_prev,
        default_unit=default_unit, page_default=fallback_page, max_report_year=max_report_year,
        period_calendar=period_calendar,
    )
    source_ref = candidates[0].locator_id if candidates else f"read.value:{enum}"
    facts: list[ScalarFact] = []
    for item in period_values:
        value = item.get("value")
        if value is None:
            # Явное отсутствие, не ноль (краевой случай карточки `read.value`).
            continue
        facts.append(ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version=ATOM_VERSION,
            source_ref=source_ref, concept=enum, document_version=document_version,
            period_ref=str(item.get("period") or period_current), value=float(value),
            unit=str(item.get("unit") or default_unit or "unknown"),
            page=item.get("page"),
        ))
    return facts


def read_value_batch(
    concepts: Sequence[str],
    candidates_by_concept: dict[str, list[CandidateLocator]],
    *,
    metrics: list[dict],
    document_version: str,
    period_current: str,
    period_prev: str,
    periods_detected: list[str] | None = None,
    has_pages: bool = True,
    default_unit: str = "",
    reporting_standard: str = "",
    company_type: str = "",
    language: str = "ru",
    period_calendar: str = "calendar",
    max_report_year: int | None = None,
    model_id: str | None = None,
) -> dict[str, list[ScalarFact]]:
    """Один логический batch для `concepts`, физически ограниченный шардами.

    Концепт без единого кандидата (`locate` ничего не нашёл) не отправляется
    модели вовсе — задавать вопрос без единой улики означает просить
    угадывать (тот же принцип, что у сегодняшнего `extraction_ask_without_
    evidence` в `two_stage_extractor`, но проще: без искусственной подмешки
    в чужие батчи)."""
    from finengine.skills.two_stage_extractor import (
        _build_main_extraction_prompt,
        _call_json_llm,
        _main_financial_metric_system_prompt,
        _recover_stage2_json_object,
        _retry_enum_order,
    )

    # Ключи `candidates_by_concept` — те же enum'ы реестра (верхний регистр),
    # что и `concept` каждого `CandidateLocator` (`locate._candidates_for_metric`
    # выставляет его явно) — то же соглашение, что и у `locate_batch`.
    wanted = {str(c).strip().upper() for c in concepts if str(c or "").strip()}
    askable = {enum for enum in wanted if candidates_by_concept.get(enum)}
    result: dict[str, list[ScalarFact]] = {c: [] for c in concepts}
    if not askable:
        return result

    target_metrics = [m for m in metrics if str(m.get("enum", "")).strip().upper() in askable]
    if not target_metrics:
        return result

    askable_candidates = {enum: candidates_by_concept[enum] for enum in askable}
    compression, table_by_id = _synthetic_evidence(askable_candidates)
    system_prompt = _main_financial_metric_system_prompt(reporting_standard, language)

    async def _read_shard(batch_no: int, shard: list[dict]) -> dict:
        retry_empty = len(shard) == 1 and shard[0].get("ad_hoc") is True
        attempts = max(1, int(settings.extract_batch_attempts))
        last_error = ""
        merged: dict = {}
        remaining = list(shard)
        empty_retries = 0
        allowed_empty_retries = attempts - 1 if retry_empty else min(1, attempts - 1)
        for attempt in range(1, attempts + 1):
            expected = [str(metric["enum"]).strip().upper() for metric in remaining]
            ordered = _retry_enum_order(
                expected, document=document_version, batch_no=batch_no, attempt=attempt,
            )
            metric_by_enum = {str(metric["enum"]).strip().upper(): metric for metric in remaining}
            attempt_metrics = [metric_by_enum[enum] for enum in ordered]
            prompt = _build_main_extraction_prompt(
                all_metrics=attempt_metrics, target_metrics=attempt_metrics,
                compression=compression, table_by_id=table_by_id,
                period_current=period_current, period_prev=period_prev,
                periods_detected=periods_detected or [], has_pages=has_pages,
                split_request=len(target_metrics) > len(shard), company_type=company_type,
                reporting_standard=reporting_standard, language=language,
            )
            try:
                _parsed, _request, raw = await _call_json_llm(
                    prompt=prompt, system=system_prompt, model_id=model_id,
                    stage=f"read_value_batch_{batch_no:03d}_attempt_{attempt:02d}",
                    debug_dir=None,
                    prompt_name=f"read_value_batch_{batch_no:03d}_attempt_{attempt:02d}_prompt",
                    response_name=f"read_value_batch_{batch_no:03d}_attempt_{attempt:02d}_response.json",
                )
            except Exception as exc:  # noqa: BLE001 — локальный retry модельной границы шарда.
                last_error = f"llm_error: {type(exc).__name__}: {exc}"
                continue
            parsed, last_error, _recovered = _recover_stage2_json_object(raw, expected)
            if not last_error:
                merged.update(parsed)
                missing = [enum for enum in expected if not _contains_extracted_value(parsed.get(enum))]
                if not missing:
                    return merged
                if empty_retries < allowed_empty_retries and attempt < attempts:
                    empty_retries += 1
                    remaining = [metric_by_enum[enum] for enum in missing]
                    last_error = "empty_extraction"
                    continue
                # Absence is a valid business outcome after the focused retry
                # budget is exhausted. Downstream gates keep it as no fact.
                return merged
        if merged:
            return merged
        raise Stage2ResponseInvalid(
            "read.value: ответ модели не прошёл валидацию "
            f"(шард {batch_no}, попыток {attempts}): {last_error or 'unknown_invalid_response'}"
        )

    async def _read_all_shards() -> list[dict]:
        semaphore = asyncio.Semaphore(max(1, int(settings.extract_batch_jobs)))

        async def _bounded(batch_no: int, shard: list[dict]) -> dict:
            async with semaphore:
                return await _read_shard(batch_no, shard)

        return await asyncio.gather(*(
            _bounded(batch_no, shard)
            for batch_no, shard in enumerate(_metric_shards(target_metrics), start=1)
        ))

    parsed: dict = {}
    for shard_result in asyncio.run(_read_all_shards()):
        parsed.update(shard_result)

    target_enums = [str(m["enum"]).strip().upper() for m in target_metrics]

    for enum in target_enums:
        raw_item = parsed.get(enum) or {}
        try:
            facts = _scalar_facts_for_concept(
                enum, raw_item, askable_candidates.get(enum, []),
                document_version=document_version, period_current=period_current, period_prev=period_prev,
                default_unit=default_unit, period_calendar=period_calendar, max_report_year=max_report_year,
            )
        except Exception:  # noqa: BLE001 — сбой разбора ОДНОГО концепта не должен унести соседей по батчу.
            # Остаётся зарегистрированным пустым списком — тот же класс исхода,
            # что и «показатель не нашёлся» (см. докстринг `_calc_formula_engine`
            # в `graph_atoms.py`): узел получает `succeeded` с нулём evidence,
            # не `failed`, чтобы не блокировать потребителей, для которых этот
            # концепт — необязательная зависимость.
            continue
        result[enum] = facts
    return result


__all__ = ["ATOM_VERSION", "Stage2ResponseInvalid", "read_value_batch"]
