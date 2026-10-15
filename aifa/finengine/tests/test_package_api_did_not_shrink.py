"""Пакет извлечения отдаёт ровно те же имена, что монолит до разбиения.

ЗАЧЕМ ЭТОТ ФАЙЛ. На последнем разрезе блок реэкспорта `streams` НЕ ВСТАВИЛСЯ:
помощник искал строку-якорь дословно, а я часом раньше дописал на неё `# noqa`.
Семь символов — включая точку входа `extract_stream_finengine_dev` — уехали в
подмодуль и пропали из пакета.

ВЕСЬ НАБОР ТЕСТОВ ЭТОГО НЕ ЗАМЕТИЛ: 86 красных / 1429 зелёных, ровно как в базе.
Поймала ручная проверка `hasattr`, которую я сделал по привычке, а не по плану.
Значит на пропажу публичного имени у нас сторожа не было вовсе — при том что
условие разбиения ровно одно: наружу пакет выглядит как прежде.

Впереди ещё дробление `streams` (там `_extract_stream_full_context` на 944
строки) и перенос двухстадийного извлечения — оба шага снова двигают символы.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать любую строку из блока `from .streams import`
в `__init__.py` — покраснеет `test_every_public_name_survived`.
"""
from __future__ import annotations

from finengine.skills import bulk_extractor as bulk

# Снято с пакета сразу после разбиения, когда набор тестов показывал ту же
# дробь, что и до него. Пополнять можно, удалять — только вместе с разбором,
# кто этим именем пользовался.
#
# 20.08: шесть имён удалены по этому правилу — `asyncio`, `contextlib`, `Path`,
# `Callable`, `Optional`, `AsyncGenerator`. Это не API извлечения, а протечка
# стандартной библиотеки в пространство имён: список снят механически с
# `dir()`, и в него попало всё, что там лежало. Разбор пользователей сделан по
# всему репозиторию (`grep` на `bulk_extractor` + фильтр по шести именам, вне
# этого файла): ноль. В самом `__init__.py` каждое встречалось РОВНО ОДИН РАЗ —
# в своей же строке импорта, под `# noqa: F401`, из-за чего линт-сторож их не
# видел. Такой же протечкой остаются `os` и `annotations`, но они в файле живые
# (`os.environ.pop`, `from __future__`), и трогать их не за что.
EXPECTED = (
    "CALCULATED_BANK",
    "CALCULATED_NON_FINANCIAL",
    "CONSOLIDATED_GROUP_DIMENSION",
    "CONSOLIDATED_GROUP_ENUM",
    "DURATION_KIND",
    "EXTRACTABLE_BANK",
    "EXTRACTABLE_NON_FINANCIAL",
    "POINT_KIND",
    "SOURCE_FORM_BALANCE",
    "SOURCE_FORM_CASH_FLOW",
    "SOURCE_FORM_EQUITY",
    "SOURCE_FORM_NOTES",
    "SOURCE_FORM_PNL",
    "_BANK_DETECTION_PHRASES",
    "_DECIMAL_COMMA_RE",
    "_DIM_MONEY",
    "_DIM_RATIO",
    "_DIM_SCALAR",
    "_DIM_TRANSPARENT_FUNCS",
    "_FORMULA_FUNCS",
    "_FORWARD_LOOKING_PERIOD_RE",
    "_FormulaError",
    "_GROUP_COMPOSITION_HEADING_KEYWORDS",
    "_GROUP_CONTEXT_KEYWORDS",
    "_GROUP_ENTITY_KEYWORDS",
    "_GROUP_EXCLUDE_KEYWORDS",
    "_GROUP_NON_SUBSIDIARY_KEYWORDS",
    "_GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS",
    "_GROUP_NOT_COMPOSITION_TABLE_KEYWORDS",
    "_GROUP_SHARE_KEYWORDS",
    "_GROUP_SUBSIDIARY_KEYWORDS",
    "_LOG",
    "_MONEY_SCALE_FACTORS",
    "_MONTHS_IN_PERIOD_RE",
    "_MONTH_PATTERN_EN",
    "_MULTIARG_CALL_RE",
    "_MissingFormulaInput",
    "_NF_DETECTION_PHRASES",
    "_NON_METRIC_DEPS",
    "_OPTIONAL_ZERO_DEPS_BY_ENUM",
    "_PARTIAL_ZERO_FORMULA_ENUMS",
    "_POINT_PERIOD_RE",
    "_RELATIVE_CURRENT_PERIOD_RE",
    "_RELATIVE_PREVIOUS_PERIOD_RE",
    "_SEGMENT_METRICS_BANK_RU",
    "_SEGMENT_METRICS_IFRS_RU",
    "_SEGMENT_METRICS_US_GAAP_EN",
    "_SOURCE_FORM_PATTERNS",
    "_SOURCE_FORM_SCAN_CHARS",
    "_TOKEN_RE",
    "_UNIT_BY_KIND",
    "_actual_values_only_rules_en",
    "_actual_values_only_rules_ru",
    "_annual_anchor_year",
    "_apply_metric_source_forms",
    "_build_avg_resolver_for_period",
    "_build_consolidated_group_context",
    "_build_segment_context",
    "_build_segment_prompt_context",
    "_build_source_form_by_page",
    "_calculate_metrics",
    "_calculate_metrics_for_periods",
    "_calculate_metrics_pair",
    "_calculated_period_kind_map",
    "_canonical_money_unit",
    "_clean_group_text",
    "_clean_segment_line",
    "_coerce_period_values",
    "_company_name_from_filename",
    "_consolidated_group_candidate_tables",
    "_contains_any",
    "_currency_from_unit_text",
    "_date_anchor_year",
    "_dedupe_equivalent_report_periods",
    "_default_unit_for_standard",
    "_detect_company_from_text",
    "_detect_company_type",
    "_detect_reporting_standard",
    "_detect_source_form_for_page",
    "_detect_year_from_text",
    "_deterministic_segment_rows_from_text",
    "_drop_periods_that_have_not_closed",
    "_duration_anchor_year",
    "_env_from_period_values",
    "_eval_calculated_formula",
    "_eval_formula_expr",
    "_eval_formula_node",
    "_extract_async",
    "_extract_consolidated_group_full_context",
    "_extract_segment_breakdown_full_context",
    "_extract_stream_full_context",
    "_extract_stream_unrestricted",
    "_filter_plausible_report_periods",
    "_financial_analyst_intro_en",
    "_financial_analyst_intro_ru",
    "_first_value",
    "_flush_group_table_block",
    "_focused_group_context",
    "_formula_allows_partial_zero",
    "_formula_env",
    "_formula_metric_tokens",
    "_group_context_relation_kind",
    "_group_percentish_count",
    "_group_row_is_non_subsidiary",
    "_group_value_for_period",
    "_has_cyrillic",
    "_has_latin",
    "_is_forward_looking_period_header",
    "_is_plausible_group_share_value",
    "_is_preferred_report_period",
    "_is_total_segment_label",
    "_iter_paginated_pages",
    "_last_keyword_pos",
    "_looks_like_auditor_name",
    "_looks_like_filename_company_label",
    "_looks_like_generic_company_label",
    "_looks_like_segment_label",
    "_looks_like_segment_number",
    "_main_total_fallback_event",
    "_max_report_anchor_year",
    "_merge_aliases_from_base",
    "_metric_block_iter",
    "_node_dimension",
    "_normalise_consolidated_group_rows",
    "_normalise_eps_unit",
    "_normalise_formula_expr",
    "_normalise_money_value",
    "_normalize_group_period_label",
    "_normalize_group_share_value",
    "_normalize_segment_context_mode",
    "_normalize_segment_mode",
    "_numeric_env_keys",
    "_optional_zero_deps_for_formula",
    "_page_from_period_values",
    "_parse_extract_json",
    "_parse_formula_expr",
    "_parse_linewise_segment_rows",
    "_parse_unit_text",
    "_parse_verbatim_value",
    "_period_end_key",
    "_period_rules_en",
    "_period_rules_ru",
    "_period_value_signature",
    "_period_year",
    "_point_period_key",
    "_prefer_detected_company_name",
    "_prefer_financial_report_periods",
    "_reconcile_company_name",
    "_registry_phrases_for_segment",
    # Переименование, а не пропажа: наш суженный
    # `_relative_duration_period_from_header` заменён их
    # `_relative_metric_period_from_header` (#257). Разбор пользователей имени
    # сделан — их было трое: реэкспорт, вызов в `period_values` и эта строка.
    "_relative_metric_period_from_header",
    "_replace_avg_calls",
    "_report_column_key",
    "_role_allows_source_forms",
    "_role_wants_segments",
    "_sanity_note",
    "_segment_metric_scope",
    "_segment_page_no",
    "_segment_periods_from_block",
    "_segment_rules_en",
    "_select_display_periods",
    "_select_previous_run_period",
    "_select_run_periods",
    "_slice_between",
    "_source_form_for_page",
    "_source_form_norm",
    "_text_looks_like_ifrs",
    "_value_for_period",
    "_verbatim_value_rules_en",
    "_verbatim_value_rules_ru",
    "_wait_for_pdf_text",
    "_with_point_values_for_duration_calculations",
    "_write_llm_debug_file",
    "annotations",
    "annualization_factor",
    "annualized",
    "detect_period_candidates",
    "extract_all_sync",
    "extract_stream_finengine_dev",
    "formula_dimension",
    "llm",
    "normalize_incomplete_duration_label",
    "normalize_metric_period",
    "normalize_period_label",
    "os",
    "period_kind_for_enum",
    "period_sort_key",
    "previous_period_label",
    "sort_periods",
    "unit_for_calculated",
)


def test_every_public_name_survived():
    """ГЛАВНОЕ: ни одно публичное имя не пропало из пакета."""
    missing = [n for n in EXPECTED if not hasattr(bulk, n)]
    assert not missing, (
        f"пакет перестал отдавать {len(missing)} имён: {missing[:10]}. "
        "Скорее всего блок реэкспорта в __init__.py не вставился или был "
        "затёрт — снаружи пакет обязан выглядеть как до разбиения")


def test_the_entry_points_are_callable():
    """Точки входа — не просто есть, а вызываемы."""
    for name in ("extract_stream_finengine_dev", "extract_all_sync"):
        assert callable(getattr(bulk, name)), f"{name} есть, но не вызываем"


def test_the_logger_kept_its_name():
    """Логгер не сменил имя при переезде в отдельный модуль.

    В монолите было `getattr(__name__)`. Напиши в `_log.py` то же самое —
    имя стало бы `...bulk_extractor._log`, и настройки уровней по прежнему
    имени перестали бы его цеплять. Тихое изменение поведения.
    """
    assert bulk._LOG.name == "finengine.skills.bulk_extractor"


def test_the_llm_binding_stays_in_the_shim():
    """`bulk_extractor.llm` остаётся: на нём держатся подмены модели в тестах.

    У Finengine это отдельный пункт плана разбиения — там ~10 тестовых файлов
    патчат `finengine.skills.bulk_extractor.llm.stream`. У нас такой подмены
    сейчас нет, но публичный API заморожен.
    """
    assert hasattr(bulk, "llm") and hasattr(bulk.llm, "stream")
