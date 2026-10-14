"""Metric extraction for Finengine pipelines.

Current `finengine` / `finengine_dev` registry mode uses full-context extraction:
the prompt receives the metric registry from `metrics.json`, asks for raw table
headers/cell values, and Python normalizes periods, values and calculated
metrics. Segment breakdowns are a second full-context LLM stage.

The older per-metric RAG path remains in this module for `finengine_rag` and
legacy validation. Its retrieval still uses METRIC_EXPANSION_ALIASES,
METRIC_ALIASES_EXTRA and METRIC_PROMPT_HINTS.
"""
from __future__ import annotations
import os

os.environ.pop("CLAUDECODE", None)

from finengine.llm import openrouter as llm  # noqa: E402,F401
from finengine.skills.metric_registry import (
    EXTRACTABLE_NON_FINANCIAL,  # noqa: F401
    EXTRACTABLE_BANK,  # noqa: F401
    CALCULATED_NON_FINANCIAL,  # noqa: F401
    CALCULATED_BANK,  # noqa: F401
)
# Часть этих имён в самом __init__ больше не используется — их потребители
# уехали в подмодули. Оставлены СОЗНАТЕЛЬНО: публичный API пакета заморожен
# на время разбиения, и `from finengine.skills.bulk_extractor import X`
# обязан работать ровно как до него. Убирать их — отдельной задачей, после.
from finengine.skills.periods import (
    DURATION_KIND,  # noqa: F401
    POINT_KIND,  # noqa: F401
    detect_period_candidates,  # noqa: F401
    normalize_incomplete_duration_label,  # noqa: F401
    normalize_period_label,  # noqa: F401
    period_kind_for_enum,  # noqa: F401
    period_sort_key,  # noqa: F401
    previous_period_label,  # noqa: F401
    sort_periods,  # noqa: F401
)
from finengine.skills.period_validation import normalize_metric_period  # noqa: F401
from .company_detection import (  # noqa: E402
    _has_latin,  # noqa: F401
    _has_cyrillic,  # noqa: F401
    _looks_like_auditor_name,  # noqa: F401
    _looks_like_generic_company_label,  # noqa: F401
    _looks_like_filename_company_label,  # noqa: F401
    _company_name_from_filename,  # noqa: F401
    _reconcile_company_name,  # noqa: F401
    _prefer_detected_company_name,  # noqa: F401
    _detect_company_from_text,  # noqa: F401
    _registry_phrases_for_segment,  # noqa: F401
    _BANK_DETECTION_PHRASES,  # noqa: F401
    _NF_DETECTION_PHRASES,  # noqa: F401
    _detect_company_type,  # noqa: F401
    _wait_for_pdf_text,  # noqa: F401
)
from .formula_engine import (  # noqa: E402
    _TOKEN_RE,  # noqa: F401
    _NON_METRIC_DEPS,  # noqa: F401
    _OPTIONAL_ZERO_DEPS_BY_ENUM,  # noqa: F401
    _PARTIAL_ZERO_FORMULA_ENUMS,  # noqa: F401
    _MissingFormulaInput,  # noqa: F401
    _formula_metric_tokens,  # noqa: F401
    _numeric_env_keys,  # noqa: F401
    _formula_allows_partial_zero,  # noqa: F401
    _optional_zero_deps_for_formula,  # noqa: F401
    _formula_env,  # noqa: F401
    _normalise_formula_expr,  # noqa: F401
    _FormulaError,  # noqa: F401
    _FORMULA_FUNCS,  # noqa: F401
    _DECIMAL_COMMA_RE,  # noqa: F401
    _MULTIARG_CALL_RE,  # noqa: F401
    _parse_formula_expr,  # noqa: F401
    _eval_formula_node,  # noqa: F401
    _eval_formula_expr,  # noqa: F401
    _DIM_MONEY,  # noqa: F401
    _DIM_RATIO,  # noqa: F401
    _DIM_SCALAR,  # noqa: F401
    _DIM_TRANSPARENT_FUNCS,  # noqa: F401
    _node_dimension,  # noqa: F401
    formula_dimension,  # noqa: F401
    _UNIT_BY_KIND,  # noqa: F401
    unit_for_calculated,  # noqa: F401
    _MONTHS_IN_PERIOD_RE,  # noqa: F401
    annualization_factor,  # noqa: F401
    annualization_note,  # noqa: F401
    _sanity_note,  # noqa: F401
    annualized,  # noqa: F401
    _with_point_values_for_duration_calculations,  # noqa: F401
    _calculated_period_kind_map,  # noqa: F401
    _env_from_period_values,  # noqa: F401
    _build_avg_resolver_for_period,  # noqa: F401
    _replace_avg_calls,  # noqa: F401
    _eval_calculated_formula,  # noqa: F401
    _calculate_metrics,  # noqa: F401
    _calculate_metrics_for_periods,  # noqa: F401
    _calculate_metrics_pair,  # noqa: F401
)
from .consolidated_group import (  # noqa: E402
    CONSOLIDATED_GROUP_ENUM,  # noqa: F401
    CONSOLIDATED_GROUP_DIMENSION,  # noqa: F401
    _GROUP_ENTITY_KEYWORDS,  # noqa: F401
    _GROUP_SHARE_KEYWORDS,  # noqa: F401
    _GROUP_CONTEXT_KEYWORDS,  # noqa: F401
    _GROUP_COMPOSITION_HEADING_KEYWORDS,  # noqa: F401
    _GROUP_SUBSIDIARY_KEYWORDS,  # noqa: F401
    _GROUP_NON_SUBSIDIARY_KEYWORDS,  # noqa: F401
    _GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS,  # noqa: F401
    _GROUP_NOT_COMPOSITION_TABLE_KEYWORDS,  # noqa: F401
    _GROUP_EXCLUDE_KEYWORDS,  # noqa: F401
    _clean_group_text,  # noqa: F401
    _contains_any,  # noqa: F401
    _group_percentish_count,  # noqa: F401
    _last_keyword_pos,  # noqa: F401
    _group_context_relation_kind,  # noqa: F401
    _focused_group_context,  # noqa: F401
    _flush_group_table_block,  # noqa: F401
    _consolidated_group_candidate_tables,  # noqa: F401
    _build_consolidated_group_context,  # noqa: F401
    _normalize_group_period_label,  # noqa: F401
    _normalize_group_share_value,  # noqa: F401
    _is_plausible_group_share_value,  # noqa: F401
    _group_value_for_period,  # noqa: F401
    _group_row_is_non_subsidiary,  # noqa: F401
    _normalise_consolidated_group_rows,  # noqa: F401
    _extract_consolidated_group_full_context,  # noqa: F401
)
from .json_parse import (  # noqa: E402
    _parse_extract_json,  # noqa: F401
)
from .segments_llm import (  # noqa: E402
    _extract_segment_breakdown_full_context,  # noqa: F401
)
from .segments_parse import (  # noqa: E402
    _is_total_segment_label,  # noqa: F401
    _period_value_signature,  # noqa: F401
    _MONTH_PATTERN_EN,  # noqa: F401
    _clean_segment_line,  # noqa: F401
    _segment_periods_from_block,  # noqa: F401
    _looks_like_segment_number,  # noqa: F401
    _looks_like_segment_label,  # noqa: F401
    _segment_page_no,  # noqa: F401
    _slice_between,  # noqa: F401
    _parse_linewise_segment_rows,  # noqa: F401
    _deterministic_segment_rows_from_text,  # noqa: F401
)
from .segments_context import (  # noqa: E402
    _SEGMENT_METRICS_IFRS_RU,  # noqa: F401
    _SEGMENT_METRICS_US_GAAP_EN,  # noqa: F401
    _SEGMENT_METRICS_BANK_RU,  # noqa: F401
    _role_wants_segments,  # noqa: F401
    _detect_reporting_standard,  # noqa: F401
    _segment_metric_scope,  # noqa: F401
    _normalize_segment_mode,  # noqa: F401
    _segment_rules_en,  # noqa: F401
    _build_segment_context,  # noqa: F401
    _normalize_segment_context_mode,  # noqa: F401
    _build_segment_prompt_context,  # noqa: F401
)
from .prompt_templates import (  # noqa: E402
    _financial_analyst_intro_ru,  # noqa: F401
    _financial_analyst_intro_en,  # noqa: F401
    _period_rules_ru,  # noqa: F401
    _period_rules_en,  # noqa: F401
    _verbatim_value_rules_ru,  # noqa: F401
    _verbatim_value_rules_en,  # noqa: F401
    _actual_values_only_rules_ru,  # noqa: F401
    _actual_values_only_rules_en,  # noqa: F401
)
from .period_values import (  # noqa: E402
    _coerce_period_values,  # noqa: F401
    _value_for_period,  # noqa: F401
    _first_value,  # noqa: F401
    _page_from_period_values,  # noqa: F401
    _main_total_fallback_event,  # noqa: F401
)
from .period_selection import (  # noqa: E402
    _drop_periods_that_have_not_closed,  # noqa: F401
    _select_run_periods,  # noqa: F401
    _is_preferred_report_period,  # noqa: F401
    _prefer_financial_report_periods,  # noqa: F401
    _report_column_key,  # noqa: F401
    _dedupe_equivalent_report_periods,  # noqa: F401
    _select_previous_run_period,  # noqa: F401
    _period_year,  # noqa: F401
    _duration_anchor_year,  # noqa: F401
    _annual_anchor_year,  # noqa: F401
    _date_anchor_year,  # noqa: F401
    _max_report_anchor_year,  # noqa: F401
    _filter_plausible_report_periods,  # noqa: F401
    _select_display_periods,  # noqa: F401
    _FORWARD_LOOKING_PERIOD_RE,  # noqa: F401
    _is_forward_looking_period_header,  # noqa: F401
    _RELATIVE_CURRENT_PERIOD_RE,  # noqa: F401
    _RELATIVE_PREVIOUS_PERIOD_RE,  # noqa: F401
    _RELATIVE_COMPARATIVE_PERIOD_RE,  # noqa: F401
    _RELATIVE_QUARTERS_BACK_RE,  # noqa: F401
    _RELATIVE_START_CURRENT_YEAR_RE,  # noqa: F401
    _RELATIVE_START_PREVIOUS_YEAR_RE,  # noqa: F401
    _RSBU_REGULATORY_PERIOD_START_ENUMS,  # noqa: F401
    _duration_from_point_anchor,  # noqa: F401
    _relative_point_anchor,  # noqa: F401
    _shift_months,  # noqa: F401
    _relative_metric_period_from_header,  # noqa: F401
    _POINT_PERIOD_RE,  # noqa: F401
    _point_period_key,  # noqa: F401
    _period_end_key,  # noqa: F401
    _detect_year_from_text,  # noqa: F401
    _model_supported_max_report_year,  # noqa: F401
)
from .value_parsing import (  # noqa: E402
    _parse_verbatim_value,  # noqa: F401
    _currency_from_unit_text,  # noqa: F401
    _default_unit_for_standard,  # noqa: F401
    _canonical_money_unit,  # noqa: F401
    _parse_unit_text,  # noqa: F401
    _normalise_eps_unit,  # noqa: F401
    _MONEY_SCALE_FACTORS,  # noqa: F401
    _normalise_money_value,  # noqa: F401
)
from .source_forms import (  # noqa: E402
    SOURCE_FORM_BALANCE,  # noqa: F401
    SOURCE_FORM_PNL,  # noqa: F401
    SOURCE_FORM_CASH_FLOW,  # noqa: F401
    SOURCE_FORM_EQUITY,  # noqa: F401
    SOURCE_FORM_NOTES,  # noqa: F401
    _SOURCE_FORM_SCAN_CHARS,  # noqa: F401
    _SOURCE_FORM_PATTERNS,  # noqa: F401
    _source_form_norm,  # noqa: F401
    _role_allows_source_forms,  # noqa: F401
    _text_looks_like_ifrs,  # noqa: F401
    _iter_paginated_pages,  # noqa: F401
    _detect_source_form_for_page,  # noqa: F401
    _build_source_form_by_page,  # noqa: F401
    _source_form_for_page,  # noqa: F401
    _apply_metric_source_forms,  # noqa: F401
)
from .debug_io import (  # noqa: E402
    _write_llm_debug_file,  # noqa: F401
)
from .streams import (  # noqa: E402
    _metric_block_iter,  # noqa: F401
    _extract_stream_unrestricted,  # noqa: F401
    _merge_aliases_from_base,  # noqa: F401
    _extract_stream_full_context,  # noqa: F401
    extract_stream_finengine_dev,  # noqa: F401
    _extract_async,  # noqa: F401
    extract_all_sync,  # noqa: F401
)

from ._log import _LOG  # noqa: E402,F401

# Bank/non-financial detection now uses phrases derived from the metric
# registry (see _registry_phrases_for_segment / _detect_company_type below).




























































































































































































































































# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter call (single-turn)
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Main streaming generator
# ─────────────────────────────────────────────────────────────────────────────





































# ─────────────────────────────────────────────────────────────────────────────
# Backward-compatible non-streaming wrapper
# ─────────────────────────────────────────────────────────────────────────────



