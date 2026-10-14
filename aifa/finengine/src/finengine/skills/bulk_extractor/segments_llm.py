"""Стадия разрезов: отдельный вызов модели по полному тексту документа.

⚠️ ЗДЕСЬ СТОЯЛ ОТКАЗ ОБСЛУЖИВАТЬ БАНКИ. Первой строкой тела было
`if effective_type == "bank": return` — стадия молча выходила для любого
банка, и включение сегментов банкам (#252) не давало ничего: флаг доезжал до
вызова, а исполнитель двумя уровнями ниже отказывался. Снято 16.08, сторож —
tests/test_segments_reach_the_bank.py, ловит разбором дерева.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator, Optional
from finengine.llm import openrouter as llm
from .debug_io import _write_llm_debug_file
from .json_parse import _parse_extract_json
from .period_selection import _max_report_anchor_year
from .period_values import _coerce_period_values, _first_value, _page_from_period_values, _value_for_period
from .prompt_templates import _actual_values_only_rules_en, _financial_analyst_intro_en, _period_rules_en, _verbatim_value_rules_en
from .segments_context import _build_segment_prompt_context, _detect_reporting_standard, _normalize_segment_context_mode, _normalize_segment_mode, _segment_metric_scope, _segment_rules_en
from .segments_parse import _deterministic_segment_rows_from_text, _is_total_segment_label, _period_value_signature
from .value_parsing import _default_unit_for_standard


async def _extract_segment_breakdown_full_context(
    *,
    pdf_text_for_prompt: str,
    extractable: list[dict],
    period_current: str,
    period_prev: str,
    company_display: str,
    effective_type: str,
    model_id: Optional[str],
    segment_mode: str = "extended",
    segment_context_mode: str = "full",
    debug_dir: Optional[Path] = None,
    segment_metrics_override: Optional[set[str]] = None,
    segment_rules_extra: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Second-stage segment/geography/product extraction for selected metrics.

    ⚠️ Здесь стоял `if effective_type == "bank": return` — стадия молча выходила
    для любого банка. Из-за него включение сегментов банкам (#252) не дало
    ничего: флаг доезжал до вызова, а исполнитель отказывался обслуживать. На
    экране это выглядело как «сегменты попросили — пришло пусто», хотя в
    сегментной записке Ак Барса (МСФО (IFRS) 8) разрезы есть и с числами.
    """
    standard = _detect_reporting_standard(pdf_text_for_prompt)
    default_unit = _default_unit_for_standard(standard)
    max_report_year = _max_report_anchor_year([period_current, period_prev])
    if segment_metrics_override is not None:
        target_metrics = [m for m in extractable if m.get("enum") in segment_metrics_override]
    else:
        target_metrics = _segment_metric_scope(extractable, standard, effective_type)
    if not target_metrics:
        return

    deterministic_rows = _deterministic_segment_rows_from_text(pdf_text_for_prompt, standard)
    context_mode = _normalize_segment_context_mode(segment_context_mode)
    context = _build_segment_prompt_context(pdf_text_for_prompt, mode=context_mode)
    if not context.strip() and not deterministic_rows:
        return

    info_blocks = []
    for m in target_metrics:
        enum = m["enum"]
        aliases = [a for a in (m.get("aliases_ru") or []) + (m.get("aliases_en") or []) if a and a != "Нет"]
        block = [
            f"- {enum}: {m.get('name_ru') or ''} / {m.get('name_en') or ''}"
            + (f"; aliases: {'; '.join(aliases[:8])}" if aliases else "")
        ]
        if m.get("description"):
            block.append(f"  description: {m['description']}")
        if m.get("hint"):
            block.append(f"  hint: {m['hint']}")
        info_blocks.append("\n".join(block))

    system_prompt = _financial_analyst_intro_en(standard)
    segment_rules = _segment_rules_en(segment_mode)
    if segment_rules_extra and segment_rules_extra.strip():
        segment_rules = segment_rules + "\n\n" + segment_rules_extra.strip()

    user_prompt = (
        f"# Company\n{company_display}\n\n"
        f"# Standard\n{standard}\n\n"
        "# Target metrics\n"
        + "\n".join(info_blocks)
        + "\n\n# Period rules\n"
        + _period_rules_en()
        + "\n\n"
        "# Verbatim value rules\n"
        + _verbatim_value_rules_en()
        + "\n\n"
        + _actual_values_only_rules_en()
        + "\n\n"
        "# Segment rules\n"
        + segment_rules
        + "\n\n"
        "Keep TOTAL rows when the table has them. For any "
        "total/summary row, set segment exactly to "
        '"TOTAL" even if the source label says Total net revenue, Total operating '
        "income, Total sales to external customers, Итого or Всего. Preserve signs "
        "for eliminations/corporate rows. If an explicit segment row exists and "
        "a value cell is blank or contains only -, —, – or −, return \"0\" for "
        "that segment value rather than null.\n\n"
        "A single segment table may disclose several target metrics at once. "
        "Do not stop after extracting revenue or profit rows; check the other "
        "rows in the same table against every target metric listed above.\n\n"
        "# Report excerpts\n"
        f"{context}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        "{\n"
        '  "segments": [\n'
        '    {"enum": "SALES_REVENUE", "segment": "North America", '
        '"dimension": "geography|operating_segment|product|other", '
        '"values": {"Year ended December 31, 2024": "123", '
        '"Year ended December 31, 2023": "111"}, '
        '"unit_text": "in millions", "page": 12}\n'
        "  ]\n"
        "}\n"
    )
    _write_llm_debug_file(debug_dir, "segment_system.md", system_prompt)
    _write_llm_debug_file(debug_dir, "segment_prompt.md", user_prompt)

    import time as _ttiming
    llm_t0 = _ttiming.time()
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=user_prompt,
            system=system_prompt,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=18000,
        )
    except Exception as e:
        yield {"type": "error", "message": f"Segment extraction LLM error: {e}"}
        return
    segment_llm_s = round(_ttiming.time() - llm_t0, 1)
    _write_llm_debug_file(debug_dir, "segment_response.json", raw)

    parsed = _parse_extract_json(raw) or {}
    rows = parsed.get("segments") if isinstance(parsed, dict) else []
    if not isinstance(rows, list):
        rows = []
    rows = [*deterministic_rows, *rows]

    metric_by_enum = {m["enum"]: m for m in target_metrics}
    # Accept US GAAP's common enum name in prompts/GT.
    if "OPERATING_PROFIT" in metric_by_enum:
        metric_by_enum["OPERATING_INCOME"] = metric_by_enum["OPERATING_PROFIT"]

    total_groups: dict[str, list[tuple[dict, list[dict], tuple[tuple[str, str], ...]]]] = {}
    non_total_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        enum = str(row.get("enum") or "").strip().upper()
        metric = metric_by_enum.get(enum)
        if not metric:
            continue
        canonical_enum = metric["enum"]
        segment = str(row.get("segment") or "").strip()
        if not segment:
            continue
        if not _is_total_segment_label(segment):
            non_total_rows.append(row)
            continue
        row = dict(row)
        row["_source_segment"] = segment
        row["segment"] = "TOTAL"
        period_values, _metric_unit = _coerce_period_values(
            row,
            enum=canonical_enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            dash_as_zero=True,
            max_report_year=max_report_year,
        )
        total_groups.setdefault(canonical_enum, []).append(
            (row, period_values, _period_value_signature(period_values))
        )

    total_diag: list[dict] = []
    for enum, candidates in total_groups.items():
        if len(candidates) == 1:
            non_total_rows.append(candidates[0][0])
            continue
        counts: dict[tuple[tuple[str, str], ...], int] = {}
        for _row, _pvs, sig in candidates:
            counts[sig] = counts.get(sig, 0) + 1
        best_sig = max(counts, key=counts.get)
        best_row = next(row for row, _pvs, sig in candidates if sig == best_sig)
        signatures_disagree = len(counts) > 1
        total_diag.append(
            {
                "enum": enum,
                "candidates": len(candidates),
                "kept_signature_count": counts[best_sig],
                "signatures_disagree": signatures_disagree,
                "source_segments": [str(row.get("_source_segment") or row.get("segment") or "") for row, _pvs, _sig in candidates],
                "signatures": [
                    {"count": count, "values": list(sig)}
                    for sig, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
                ],
            }
        )
        non_total_rows.append(best_row)
    rows = non_total_rows

    sent = 0
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        enum = str(row.get("enum") or "").strip().upper()
        metric = metric_by_enum.get(enum)
        if not metric:
            continue
        canonical_enum = metric["enum"]
        segment = str(row.get("segment") or "").strip()
        if not segment:
            continue
        dimension = str(row.get("dimension") or "other").strip() or "other"
        period_values, metric_unit = _coerce_period_values(
            row,
            enum=canonical_enum,
            period_current=period_current,
            period_prev=period_prev,
            default_unit=default_unit,
            abs_cost=False,
            dash_as_zero=True,
            max_report_year=max_report_year,
        )
        if not period_values:
            continue
        key = (canonical_enum, segment.lower(), dimension.lower())
        if key in seen:
            continue
        seen.add(key)
        v_cur = _value_for_period(period_values, period_current)
        if v_cur is None:
            v_cur = _first_value(period_values)
        v_prv = _value_for_period(period_values, period_prev)
        yield {
            "type": "metric",
            "enum": canonical_enum,
            "name_ru": metric.get("name_ru") or metric.get("name_en") or canonical_enum,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": period_values[0].get("unit") or metric_unit,
            "found": any(p.get("value") is not None for p in period_values),
            "is_calculated": False,
            "formula": "",
            "page": _page_from_period_values(period_values),
            "segment": segment,
            "dimension": dimension,
        }
        sent += 1

    if usage:
        from finengine.skills.pricing import cost_usd
        usage_model = str(usage.get("model") or model_id or "")
        tokens_in = int(usage.get("in") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("out") or usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        tokens_cached = int(usage.get("cached") or 0)
        yield {
            "type": "segment_diag",
            "_diag_segment": {
                "segment_rows": sent,
                "segment_mode": _normalize_segment_mode(segment_mode),
                "segment_context_mode": context_mode,
                "segment_context_chars": len(context),
                "segment_standard": standard,
                "segment_deterministic_rows": len(deterministic_rows),
                "segment_total_dedupe": total_diag,
                "segment_llm_s": segment_llm_s,
                "segment_tokens_in": tokens_in,
                "segment_tokens_out": tokens_out,
                "segment_tokens_cached": tokens_cached,
                "llm_requests": [
                    {
                        "stage": "segment",
                        "model": usage_model,
                        "elapsed_s": segment_llm_s,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "cost_usd": round(cost_usd(usage_model, tokens_in, tokens_out, tokens_cached), 4),
                    }
                ],
            },
        }
