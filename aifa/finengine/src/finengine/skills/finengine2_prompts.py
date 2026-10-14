"""Prompt profiles for the Finengine 2 two-stage extractor.

Finengine 2 deliberately owns its prompts instead of inheriting the current
Finengine cell prompts.  Every matrix cell is registered explicitly so a cell
can move to a dedicated language/domain prompt later without changing routing,
registries, or extractor code.  For now all cells use the benchmarked generic
v1 prompt family; its rule language follows the document language.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finengine2PromptProfile:
    profile_id: str
    family: str
    reporting_standard: str
    language: str
    guardrails: tuple[str, ...] = ()
    segment_guardrails: tuple[str, ...] = ()


_CELL_AXES: dict[str, tuple[str, str]] = {
    "ifrs_nf_ru": ("IFRS", "ru"),
    "ifrs_bank_ru": ("IFRS", "ru"),
    "rsbu_nf_ru": ("РСБУ", "ru"),
    "rsbu_bank_ru": ("РСБУ / правила Банка России", "ru"),
    "us_gaap_nf_en": ("US GAAP", "en"),
    "us_gaap_bank_en": ("US GAAP", "en"),
    "ifrs_nf_en": ("IFRS", "en"),
    "ifrs_bank_en": ("IFRS", "en"),
    "ind_as_nf_en": ("Ind AS", "en"),
    "ind_gaap_bank_en": ("Indian GAAP", "en"),
}

PROMPT_FAMILY_GENERIC_V1 = "generic_v1"

_CELL_GUARDRAILS: dict[str, tuple[str, ...]] = {
    "rsbu_nf_ru": (
        "Use Russian statutory accounting statements and forms 0710001/0710002/0710004; do not substitute IFRS consolidated figures.",
        "Treat visible Russian form and line codes as source evidence, while still requiring an exact registry concept match.",
        "Preserve literal Russian period headers, source signs and units such as тыс. руб.; do not convert to millions.",
        "Never use a metadata/code row such as `Коды 0710001 31 | 12 | 2025 ...` as a period key; use the exact date header aligned with the numeric column.",
        "A source cell `214 894` under `тыс. руб.` must stay `214 894` with unit_text `тыс. руб.`; never rewrite it as `214.894` in millions.",
    ),
    "rsbu_bank_ru": (
        "Use published statutory bank statements and Bank of Russia forms; do not substitute IFRS consolidated figures or infer IFRS 9 stages.",
        "Distinguish gross and net loans and corporate and retail portfolios only from explicitly separate source rows.",
        "Prefer dedicated Bank of Russia tables for regulatory capital and ratios, and preserve literal periods, signs and units.",
        "Never use a metadata/code row such as `Коды 0409806 31 | 12 | 2025 ...` as a period key; use the exact date or relative column header aligned with the numeric cell.",
        "A source cell `214 894` under `тыс. руб.` must stay `214 894` with unit_text `тыс. руб.`; never rewrite it as `214.894` in millions.",
    ),
    "ind_as_nf_en": (
        "Use consolidated statutory Ind AS disclosures; do not substitute standalone-company or non-Ind-AS columns.",
        "Preserve Indian reporting units and raw March fiscal-year headers exactly as shown in the source.",
        "Prefer statutory reported figures over management-adjusted presentations unless the metric registry explicitly requests an adjusted measure.",
    ),
    "ind_gaap_bank_en": (
        "Use consolidated statutory Indian GAAP/RBI disclosures; do not substitute standalone-company columns.",
        "Respect Indian banking and RBI terminology; do not force IFRS 9 stage or US GAAP CECL labels onto a different statutory disclosure.",
        "Use dedicated regulatory tables for capital, liquidity and asset-quality metrics when they are the primary disclosure.",
        "Preserve Indian reporting units and raw March fiscal-year headers exactly as shown in the source.",
    ),
}

_CELL_SEGMENT_GUARDRAILS: dict[str, tuple[str, ...]] = {
    "rsbu_nf_ru": (
        "Extract only explicit business, product or geography breakdowns; exclude totals, intragroup turnover, eliminations, adjustments and reconciliation rows.",
        "For revenue, prefer sales to external customers when separately disclosed.",
    ),
    "ifrs_nf_ru": (
        "Treat `Исключения`, `Элиминации`, `Корректировки`, `Межсегментная деятельность` and equivalent Russian labels as the prohibited reconciliation mechanics above.",
        "For segment revenue, prefer revenue from external customers. Use total segment revenue only when an external-customer row or column is not disclosed.",
    ),
    "ifrs_nf_en": (
        "For segment revenue, prefer revenue from external customers. Use total segment revenue only when an external-customer row or column is not disclosed.",
    ),
    "us_gaap_nf_en": (
        "Prefer statutory reportable-segment disclosures and GAAP measures over adjusted or non-GAAP segment reconciliations.",
    ),
}


def prompt_profile_for_cell(profile_id: str | None) -> Finengine2PromptProfile:
    normalized = str(profile_id or "").strip().lower()
    standard, language = _CELL_AXES.get(normalized, ("financial reporting", "en"))
    return Finengine2PromptProfile(
        profile_id=normalized or "generic",
        family=PROMPT_FAMILY_GENERIC_V1,
        reporting_standard=standard,
        language=language,
        guardrails=_CELL_GUARDRAILS.get(normalized, ()),
        segment_guardrails=_CELL_SEGMENT_GUARDRAILS.get(normalized, ()),
    )


def _guardrail_block(profile: Finengine2PromptProfile) -> str:
    if not profile.guardrails:
        return ""
    return "\n".join(f"- {rule}" for rule in profile.guardrails) + "\n"


def _segment_guardrail_block(profile: Finengine2PromptProfile) -> str:
    if not profile.segment_guardrails:
        return ""
    return "\n".join(f"- {rule}" for rule in profile.segment_guardrails) + "\n"


def build_system_prompt(profile: Finengine2PromptProfile) -> str:
    standard = profile.reporting_standard
    if profile.language == "ru":
        return (
            f"Ты опытный финансовый аналитик. Работаешь с финансовым отчетом стандарта {standard}. "
            "Твоя задача — находить и извлекать обычные финансовые показатели из отчетности, "
            "строго сопоставляя строки отчета с описаниями, синонимами и подсказками реестра. "
            "Если строка похожа на показатель, но шире, уже или включает лишние компоненты, не используй ее. "
            "Возвращай только валидный JSON."
        )
    return (
        f"You are a senior financial analyst working with {standard} financial reports. "
        "Your task is to find and extract ordinary financial statement metrics, matching source rows "
        "strictly against each metric's registry description, aliases and hint. "
        "If a row is similar but broader, narrower, or includes extra components, do not use it. "
        "Return valid JSON only."
    )


def _period_rules(language: str) -> str:
    if language == "ru":
        return (
            "## Правила периодов\n"
            "Возвращай ключ периода в `values` буквально как в видимой шапке таблицы; "
            "не сокращай и не перефразируй его. "
            "Шапка может быть двухуровневой: на одном уровне группа периода "
            "(например, «За три месяца»), на другом — дата окончания. Объединяй "
            "применимые уровни в один полный ключ периода. Если одна дата повторяется "
            "в разных группах периодов, голая дата недопустима: она создаёт дублирующий "
            "ключ JSON; возвращай полные формулировки для обеих групп."
        )
    return (
        "Return period keys in `values` literally as they appear in the visible "
        "table header; do not shorten or rephrase them. A table header may have two "
        "levels: a duration group such as 'Three Months Ended' on one level and the "
        "period-end date on another. Combine the applicable levels into one complete "
        "period key. When the same date appears under multiple duration groups, a bare "
        "date is invalid because it creates a duplicate JSON key; return the full "
        "wording for each group."
    )


def _verbatim_value_rules(language: str) -> str:
    if language == "ru":
        return (
            "## КРИТИЧЕСКОЕ правило: возвращай значение БУКВАЛЬНО как написано в источнике\n"
            "В объекте `values` каждое значение должно быть строкой **точно как в ячейке "
            "таблицы PDF** — со всеми пробелами, запятыми, скобками. НЕ конвертируй "
            "единицы, НЕ нормализуй разделители, НЕ удаляй скобки. Деривация числа — "
            "задача детерминированного кода после тебя, не твоя.\n\n"
            "Поле `unit_text` — буквально из заголовка таблицы / шапки колонки / шапки "
            "документа («in millions», «$ in millions», «в млрд руб.», «тыс. руб.», "
            "«руб.», «%»). Бери ближайший видимый источник единицы; для разных метрик "
            "она может различаться. Не подставляй рубли для US GAAP / USD-отчётности.\n\n"
            "Если значение взято НЕ ИЗ ТАБЛИЦЫ, а из фразы примечания, единицу бери "
            "ИЗ ЭТОЙ ЖЕ ФРАЗЫ, а не из шапки документа: примечания сплошь и рядом "
            "называют свой масштаб («84,7 млрд рублей») в отчёте, свёрстанном в "
            "миллионах. Шапка документа в этом случае — чужая единица, и число под "
            "ней уменьшается в тысячу раз.\n"
            "ИСТОЧНИК (отчёт в миллионах): «Группа имела договорные обязательства "
            "по закупкам капитального характера на сумму 84,7 млрд рублей»\n"
            "WRONG: {\"values\": {\"31.03.2025\": \"84,7\"}, "
            "\"unit_text\": \"в миллионах рублей\"}\n"
            "RIGHT: {\"values\": {\"31.03.2025\": \"84,7\"}, "
            "\"unit_text\": \"млрд рублей\"}\n\n"
            "### ПРИМЕРЫ\n\n"
            "ИСТОЧНИК: «Чистый процентный доход | 2 999,6 | 2 564,6» под заголовком "
            "«(в миллиардах российских рублей)»\n"
            "WRONG: {\"values\": {\"FY 2024\": \"2999600000\"}, \"unit_text\": \"млн руб\"}\n"
            "WRONG: {\"values\": {\"FY 2024\": \"2999.6\"}, \"unit_text\": \"млн руб\"}\n"
            "RIGHT: {\"values\": {\"За год, закончившийся 31 декабря 2024 года\": "
            "\"2 999,6\", \"За год, закончившийся 31 декабря 2023 года\": \"2 564,6\"}, "
            "\"unit_text\": \"в миллиардах российских рублей\"}\n\n"
            "ИСТОЧНИК: «Расходы по налогу | (454,3) | (250,0)» в той же таблице\n"
            "WRONG: {\"values\": {\"FY 2024\": \"-454.3\"}} или "
            "{\"values\": {\"FY 2024\": \"454.3\"}}\n"
            "RIGHT: {\"values\": {\"За год, закончившийся 31 декабря 2024 года\": "
            "\"(454,3)\", \"За год, закончившийся 31 декабря 2023 года\": \"(250,0)\"}}\n"
            "Если в ячейке скобки, они обязательны: `(465)` -> `(465)`, не `465`.\n\n"
            "ИСТОЧНИК: «Дивиденды на акцию | 35 | 30» под заголовком «(в рублях)»\n"
            "RIGHT: {\"values\": {\"2024\": \"35\"}, \"unit_text\": \"в рублях\"}\n\n"
            "Если строка нужной метрики найдена, но ячейка периода содержит прочерк "
            "или пустое значение как отображаемый числовой placeholder, верни строку "
            "\"0\" для этого периода. Это правило действует только когда название "
            "строки отдельно и однозначно соответствует метрике; прочерк в более широкой "
            "составной строке не доказывает нулевое значение её компонентов. Если это не "
            "числовой placeholder либо самой "
            "строки/показателя в отчёте нет — используй null или пустой `values`."
        )
    return (
        "Return each value exactly as it appears in the table cell. Do NOT convert "
        "units, do NOT normalize thousands separators, do NOT remove commas, spaces "
        'or parentheses. If the table says "25,785", return "25,785", not "25785" '
        'and not "25.785". If the table says "(4,190)", return "(4,190)"; '
        'parentheses are mandatory: "(465)" must stay "(465)", not "465". '
        "If a matching metric row uses a blank or dash-like cell as a displayed "
        "numeric placeholder, return \"0\" for that period, but only when that row "
        "separately and unambiguously matches the metric. A dash in a broader combined "
        "row does not prove that any component is zero. Otherwise use null "
        "when the row is absent or the value is not disclosed. "
        "Put the table/header unit literally into unit_text, for example "
        '"in millions" or "$ in millions". '
        "When the value comes from a narrative sentence in a note rather than from a "
        "table, take the unit from that same sentence, not from the document header: "
        "notes routinely name their own scale (\"USD 12.0 billion\") inside a report "
        "presented in millions, and the document header would shrink the number a "
        "thousandfold. Source (report in millions): \"committed credit facilities of "
        "$12.0 billion\" → "
        '{"values": {"27.09.2025": "12.0"}, "unit_text": "billion"}, '
        'not "in millions".'
    )


def _actual_values_rules(language: str) -> str:
    if language == "ru":
        return (
            "## Только фактические значения\n"
            "Извлекай только фактические исторические/отчётные значения из финансовой "
            "отчётности и примечаний за уже завершившиеся периоды. Не извлекай прогнозы, "
            "планы, бюджеты, ориентиры, цели, сценарии, sensitivity analysis, guidance, "
            "outlook, forecast, projection, target и любые forward-looking значения, даже "
            "если строка и метрика похожи на нужные. Если нужная метрика встречается только "
            "в прогнозной таблице или forecast/outlook/guidance-разделе — верни null / "
            "пустой values.\n"
            "Не считай прогнозом обычные фактические бухгалтерские оценки внутри отчётности "
            "(например ECL/ожидаемые кредитные убытки, оценочные обязательства, отложенные "
            "налоги или пенсионные расчёты), если они отражены как фактические значения "
            "за отчётный период.\n"
            "То же и про колонки: прогнозный период в шапке (2026П, FY26E, «план») "
            "в values не переноси."
        )
    return (
        "## Actual values only\n"
        "Extract only actual historical/reported values from financial statements "
        "and notes for periods that have already ended. Do NOT extract forecasts, "
        "plans, budgets, targets, scenarios, sensitivity analysis, guidance, outlook, "
        "projections, projected/forecasted values or any other forward-looking "
        "amounts, even when the row label looks like a target metric. If a requested "
        "metric appears only in a forecast/outlook/guidance table or section, return "
        "null / an empty values object for it.\n"
        "Do not treat ordinary accounting estimates inside actual financial "
        "statements as forecasts: expected credit losses, provisions, deferred taxes "
        "and pension assumptions may be valid actual-period disclosures when they are "
        "reported in the statements or notes.\n"
        "The same applies to columns: a forecast period in the header (2026E, FY26F, "
        '"plan") does not belong in values.'
    )


def build_compression_prompt(
    profile: Finengine2PromptProfile,
    *,
    ocr_text: str,
    table_catalog: str,
    registry_json: str,
    output_template: str,
) -> str:
    return (
        "<task>\n"
        "You are doing recall-first context compression only. For each requested financial metric, "
        "find all exact source evidence that a later extraction step can read.\n"
        "Return up to 10 table ids and up to 10 exact non-table narrative quotes per metric. "
        "Prioritize full coverage: include every table or quote that discloses the exact metric "
        "for any period, including comparative periods, repeated statements, note tables, "
        "roll-forward/reconciliation tables, and adjacent fragments split across pages. "
        "Quotes must be short: 1-2 sentences copied from the report. Do not extract metric values yet.\n"
        "</task>\n\n"
        f"<ocr_text>\n{ocr_text}\n</ocr_text>\n\n"
        f"<ocr_tables>\n{table_catalog or 'No markdown tables detected.'}\n</ocr_tables>\n\n"
        f"<registry>\n{registry_json}\n</registry>\n\n"
        "<rules>\n"
        "- Optimize for evidence recall, not for choosing one best source. Missing a true source is worse than returning several exact source ids.\n"
        "- Select evidence for the exact metric described in the registry, not for a merely similar row.\n"
        "- Use numeric table ids exactly as shown in <ocr_tables>, for example 0.\n"
        "- A useful table may contain several requested metrics; include it for every matching metric.\n"
        "- Include all relevant repeated or adjacent table ids when the report splits one logical disclosure across periods, pages, statements, notes, or reconciliation tables.\n"
        "- For metrics disclosed in prose rather than tables, return exact quotes outside tables.\n"
        "- Prefer explicit metric sources. If none are explicit, include sources that help determine that the metric is absent, not separately disclosed, or should be null.\n"
        "- If neither explicit nor absence/null evidence exists, include the closest candidate tables or quotes so the next step can judge relevance.\n"
        "- Return empty arrays only when the report contains no useful explicit, absence/null, or candidate evidence for the metric.\n"
        "- Before returning JSON, silently do a final recall pass: for each metric, scan the table catalogue again for the metric name, aliases, and conceptually exact row labels in primary statements, notes, roll-forwards, and reconciliation tables. Add any missed exact table ids. Do not describe this check in the JSON.\n"
        "- Do not calculate, normalize periods, normalize signs, or copy numeric values as the final answer.\n"
        + _guardrail_block(profile)
        + "</rules>\n\n"
        "<output_format>\n"
        "Return exactly one valid JSON object only. No markdown wrappers, comments, or trailing text.\n"
        "Schema:\n"
        "{\n"
        '  "metrics": {\n'
        '    "ENUM": {\n'
        '      "tables": [{"table_id": "0", "location": "Consolidated statement of financial position"}],\n'
        '      "quotes": [{"page": 12, "text": "A short exact quote copied from narrative text outside tables."}]\n'
        "    }\n"
        "  }\n"
        "}\n\n"
        "Use this prefilled JSON template and replace placeholders with real evidence or empty arrays:\n"
        f"{output_template}\n</output_format>\n"
    )


def build_extraction_prompt(
    profile: Finengine2PromptProfile,
    *,
    target_enums: list[str],
    metric_blocks_json: str,
    schema_lines: list[str],
    split_request: bool,
    company_type: str,
) -> str:
    target_header = "# TARGET_ENUMS\n" + ", ".join(target_enums) + "\n\n" if split_request else ""
    target_rule = (
        "Extract values only for TARGET_ENUMS. The response JSON must contain only TARGET_ENUMS.\n"
        if split_request
        else "Extract values for every metric listed in the metric metadata below.\n"
    )
    schema_intro = (
        "A JSON object whose keys exactly match TARGET_ENUMS:"
        if split_request
        else "A JSON object whose keys exactly match the metric metadata list below:"
    )
    rules = (
        _period_rules(profile.language)
        + "\n\n"
        + _verbatim_value_rules(profile.language)
        + "\n\n"
        + _actual_values_rules(profile.language)
        + "\n\n"
        "- Follow each metric registry entry strictly. A similar-looking row is not enough.\n"
        "- Before extracting a value, run a sanity check: the row label and nearby table context must match the metric name/aliases and satisfy the description and hint.\n"
        "- Perform a scope check against the registry definition: do not use a narrower component, subcategory or subtotal that is only part of the requested metric; do not use a broader or combined row that contains the requested metric together with other components. Use only the exact requested metric, or an aggregation explicitly permitted by that metric's registry hint.\n"
        "- Extract all periods disclosed in the selected evidence: all years, quarters, half-years/YTD periods, rolling or shifted periods, and comparative periods.\n"
        "- After you identify a row/table that matches a metric, scan all period columns in that row and all evidence tables/quotes for the same metric. Return every explicitly disclosed period for that metric, not only the current period or first matching table. If quarterly and YTD/half-year rows are both disclosed, return both sets.\n"
        "- In a roll-forward or movement table with opening balance, changes, and closing/end-of-period balance, a point-in-time metric for the requested date comes from its matching subcolumn under the closing/end-of-period group. For example, carrying amount means closing carrying amount, not opening carrying amount, initial/gross cost, additions, amortization, or impairment. Keep the period from the matching row.\n"
        "- Do not mix currencies, units, or scales. If a table has multiple column sets, choose one coherent set for the metric. Prefer the report's primary presentation/native currency columns over convenience translation columns when both are shown. Currency/unit/scale may be written in the caption, unit line, or directly near the period column headers; preserve the chosen unit/currency in unit_text and raw headers when present.\n"
        "- The registry is the definition, not a keyword list. Accept a row only when its full label, nearby context, description and hint agree; do not broaden a metric from a partial alias or a similar subtotal.\n"
        "- Do not calculate values, allocate totals, infer missing periods, or normalize signs/period labels yourself, unless the specific metric registry entry explicitly authorizes that derivation. For an authorized derivation, use only the stated method and operands explicitly present in this evidence.\n"
        "- If you are unsure whether the row matches the registry definition, prefer null over a bold guess.\n"
        + _guardrail_block(profile)
    )
    return (
        "## Task\n"
        "Extract reported financial metric values from compressed evidence selected in stage 1.\n"
        f"Company type: {company_type or 'unknown'}; reporting standard: {profile.reporting_standard}; language: {profile.language}.\n"
        + target_rule
        + "For each metric block, read the registry fields and evidence, then extract every value of that exact metric for every disclosed period.\n"
        "Return raw period headers and raw cell values literally as they appear in the report.\n\n\n"
        + target_header
        + "## Metric Blocks\n"
        + metric_blocks_json
        + "\n\n## Rules\n"
        + rules
        + "\n\n## Output Format\n"
        "Return exactly one valid JSON object only. No explanations, no markdown wrappers, no comments.\n"
        f"{schema_intro}\n\n{{\n  " + ",\n  ".join(schema_lines) + "\n}\n"
    )


def build_segment_compression_prompt(
    profile: Finengine2PromptProfile,
    *,
    ocr_text: str,
    table_catalog: str,
    registry_json: str,
    output_template: str,
) -> str:
    return (
        "<task>\n"
        "You are doing recall-first context compression for a later business-breakdown extraction step. "
        "For each requested financial metric, collect every table that may disclose that metric across "
        "multiple business categories. Also include tables that break down a closely related or similarly "
        "named financial measure, because the later step will decide whether it matches the registry exactly. "
        "Return every candidate table id, ordered from most to least relevant, and up to 10 short exact "
        "narrative quotes per metric. Do not extract values or decide which breakdown categories qualify yet.\n"
        "</task>\n\n"
        f"<ocr_text>\n{ocr_text}\n</ocr_text>\n\n"
        f"<ocr_tables>\n{table_catalog or 'No markdown tables detected.'}\n</ocr_tables>\n\n"
        f"<registry>\n{registry_json}\n</registry>\n\n"
        "<rules>\n"
        "- Optimize for recall. Missing a potentially useful breakdown table is worse than returning a plausible extra candidate.\n"
        "- Include every repeated, adjacent or split table fragment needed to cover current and comparative periods.\n"
        "- A table may be evidence for several requested metrics; include it under every applicable enum.\n"
        "- Include exact metric disclosures first, then close financial concepts whose row labels or meaning could plausibly be confused with the requested metric.\n"
        "- Use numeric table ids exactly as shown in <ocr_tables>.\n"
        "- Quotes must be exact 1-2 sentence excerpts outside tables and are useful only when they describe a quantitative business breakdown or explain a relevant table.\n"
        "- Return empty arrays only when the report contains no useful exact or close candidate evidence.\n"
        "- Do not extract values, calculate, normalize periods or signs, or reject a table merely because its breakdown may later be out of scope.\n"
        + _guardrail_block(profile)
        + "</rules>\n\n"
        "<output_format>\n"
        "Return exactly one valid JSON object only. No markdown wrappers, comments or trailing text.\n"
        "Schema:\n"
        '{"metrics":{"ENUM":{"tables":[{"table_id":"0","location":"Business breakdown note"}],'
        '"quotes":[{"page":12,"text":"A short exact narrative quote outside tables."}]}}}\n\n'
        "Use this prefilled JSON template and replace placeholders with real evidence or empty arrays:\n"
        f"{output_template}\n</output_format>\n"
    )


def _segment_scope_rules(mode: str) -> str:
    common = (
        "Extract only company-level business breakdowns that the report presents as operating/reportable "
        "segments, products or product groups/business lines, or geography/regions/markets. Do not treat "
        "payment methods, customer settlement channels, legal entities, sales channels or incidental "
        "administrative splits as business segments. A breakdown nested inside one parent segment or product "
        "must not be promoted to a company-level row without preserving its parent context."
    )
    if mode == "core":
        return (
            common
            + "\n- Prefer flat disclosures that show one eligible business dimension at a time. Do not return "
            "matrix/pivot cells or nested cross-breakdowns where rows and columns represent different eligible "
            "dimensions when the exact metric is also available in a flat disclosure. If a cross-breakdown is "
            "the report's only disclosure of the exact metric, return it and preserve every level in the label."
        )
    return (
        common
        + "\n- Return both flat disclosures and cross-breakdowns. A cross-breakdown intersects two or more "
        "eligible dimensions in matrix/pivot cells or nested rows. Preserve all intersected labels from outer "
        "to inner and join them with ` | `, for example `Europe | Consumer | Service plans`."
    )


def build_segment_extraction_prompt(
    profile: Finengine2PromptProfile,
    *,
    mode: str,
    target_enums: list[str],
    metric_blocks_json: str,
    output_template: str,
    company_type: str,
) -> str:
    normalized_mode = "core" if str(mode or "").strip().lower() == "core" else "all"
    return (
        "## Task\n"
        "Extract business-breakdown rows from the stage-1 evidence below. Each metric block contains one "
        "registry entry and candidate tables or quotes. Candidate evidence is intentionally broad: return rows "
        "only when the disclosed financial measure is exactly the requested registry metric, not merely similar.\n"
        f"Company type: {company_type or 'unknown'}; reporting standard: {profile.reporting_standard}; language: {profile.language}.\n"
        "For every requested enum, inspect all evidence tables and return every qualifying row and every "
        "disclosed period. Use an empty `rows` array when the exact metric has no qualifying breakdown.\n\n"
        "## Target Enums\n"
        + ", ".join(target_enums)
        + "\n\n## Metric Blocks\n"
        + metric_blocks_json
        + "\n\n## Rules\n"
        + _segment_scope_rules(normalized_mode)
        + "\n- Treat the registry description, aliases, exclusions and hint as the definition. Reject a narrower "
        "component, a broader combined measure, and a similar subtotal unless the registry explicitly allows it.\n"
        "- Inspect every row and column of each evidence table: one table may disclose several requested metrics.\n"
        "- Return all current and comparative years, quarters, YTD, rolling or shifted periods disclosed for each qualifying row.\n"
        "- Return raw period headers and raw cell values literally as shown. Preserve signs, parentheses, separators, units and scales.\n"
        "- If an explicit qualifying row uses a dash or blank cell as a displayed numeric placeholder, return `0`, but only when that row separately and unambiguously matches the metric; a broader combined row does not prove a component is zero. Otherwise omit an undisclosed value.\n"
        "- Keep explicit total rows and set their segment label exactly to `TOTAL`.\n"
        "- Do not return reconciliation, elimination, adjustment, intersegment, unallocated balancing or consolidation-mechanics rows as business segments.\n"
        "- Do not calculate, allocate or infer segment values unless the specific metric registry entry explicitly authorizes that derivation.\n"
        "- Use only these dimension values: `operating_segment`, `product`, `geography`, `other`.\n"
        "- Never emit an enum outside the requested target list and never duplicate the same enum, segment, dimension and period.\n"
        + _segment_guardrail_block(profile)
        + "\n## Output Format\n"
        "Return exactly one valid JSON object only. Its `metrics` keys must exactly match the target enum list. "
        "No explanations, markdown wrappers or comments.\n"
        "Each enum contains a `rows` array; use an empty array when nothing qualifies.\n"
        "Use this prefilled template and replace placeholders with actual rows or empty arrays:\n"
        + output_template
        + "\n"
    )
__all__ = [
    "Finengine2PromptProfile",
    "PROMPT_FAMILY_GENERIC_V1",
    "build_compression_prompt",
    "build_extraction_prompt",
    "build_segment_compression_prompt",
    "build_segment_extraction_prompt",
    "build_system_prompt",
    "prompt_profile_for_cell",
]
