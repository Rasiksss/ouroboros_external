"""Legacy prompt assemblies — the byte-for-byte russian NF-IFRS prompts
that `bulk_extractor._extract_stream_full_context` used to build inline.

Phase D-1/2/3 introduced cell-owned prompt builders for `ifrs_nf_ru`.
Phase D-4 extends the same pattern to the remaining 5 cells without
changing their behavior — every cell currently calls these helpers
verbatim. When a cell wants to diverge (e.g. switch to english prompts
for `us_gaap_nf_en`), it overrides its own `_build_system_prompt` /
`_build_user_prompt` and stops calling here.

Source of truth for the prompt strings is THIS module from now on.
bulk_extractor's inline assembly is the "no override" fallback for
the unrestricted / baseline arches that don't go through the matrix.
"""

from __future__ import annotations

from finengine.skills.extractors._shared.prompts import (
    actual_values_only_rules_en,
    actual_values_only_rules_ru,
    financial_analyst_intro_en,
    financial_analyst_intro_ru,
    period_rules_en,
    period_rules_ru,
    verbatim_value_rules_en,
    verbatim_value_rules_ru,
)


def _non_financial_industry_alias_note_ru() -> str:
    return (
        "\n\n# Отраслевые названия строк для нефинансовых компаний\n"
        "Держи в уме род деятельности компании: часть метрик может называться "
        "не канонически, а через бизнес-модель эмитента. Используй отраслевое "
        "название только если оно совпадает с описанием метрики и стоит в "
        "правильном разделе отчёта.\n"
        "Few-shot:\n"
        "- Ритейлер: строка `Cost of merchandise sold` сразу после revenue и "
        "перед gross profit может быть COSTS_EXPENSES.\n"
        "- Software/SaaS: строки `Cost of subscription revenue` и `Cost of "
        "professional services revenue` можно суммировать для COSTS_EXPENSES, "
        "если они являются прямыми затратами на раскрытую выручку.\n"
        "- Не делай так: SG&A, R&D, depreciation или общий subtotal с "
        "operating expenses не являются COSTS_EXPENSES, даже если находятся "
        "рядом в P&L."
    )


def _non_financial_industry_alias_note_en() -> str:
    return (
        "\n\n# Industry-specific row names for non-financial companies\n"
        "Keep the company's line of business in mind: some metrics may be "
        "labelled through the issuer's business model rather than by the "
        "canonical registry name. Use an industry-specific label only when "
        "it matches the metric description and appears in the right statement "
        "section.\n"
        "Few-shot:\n"
        "- Retailer: `Cost of merchandise sold` immediately after revenue and "
        "before gross profit can be COSTS_EXPENSES.\n"
        "- Software/SaaS: `Cost of subscription revenue` plus `Cost of "
        "professional services revenue` can be summed for COSTS_EXPENSES when "
        "they are direct costs of the disclosed revenue streams.\n"
        "- Do not do this: SG&A, R&D, depreciation or a broad subtotal that "
        "includes operating expenses is not COSTS_EXPENSES, even if it sits "
        "nearby in the P&L."
    )


def build_legacy_ru_system_prompt(reporting_standard: str) -> str:
    """Russian NF/IFRS system prompt — exactly what legacy built inline."""
    return (
        financial_analyst_intro_ru(reporting_standard)
        + "\n\n"
        + verbatim_value_rules_ru()
        + "\n\n"
        + period_rules_ru()
        + "\n\n"
        + actual_values_only_rules_ru()
        + "\n\n"
        "## Прочие правила извлечения\n"
        "1. **Лучше null, чем неверное число.** Если не уверен на 100%, что нашёл "
        "именно эту метрику — ставь null. Ложное значение хуже пропуска.\n"
        "2. **Для main metrics сначала используй основные финансовые формы.** "
        "Предпочитай Statement of operations / balance sheet / cash flows и их "
        "IFRS-аналоги. MD&A, сегментные и non-GAAP таблицы используй только если "
        "основная форма не раскрывает нужную метрику напрямую. Если одна и та же "
        "строка есть в основной форме со скобками/минусом и в MD&A положительным "
        "числом, бери signed-значение из основной формы.\n"
        "3. **Сначала найди раздел отчёта, где должна быть метрика.** Используй "
        "поле 'Где искать' для каждой метрики.\n"
        "4. **Сверяй заголовки строк с описанием метрики.** Не путай похожие: "
        "«Себестоимость продаж» ≠ «Операционные расходы»; «Финансовые доходы» ≠ "
        "«Процентные доходы банка».\n"
        "5. **Извлекай все периоды, которые есть в строке/таблице для метрики.** "
        "Не ограничивайся двумя столбцами или primary/prior-периодами. Если строка "
        "раскрывает сравнительный прошлый год/квартал/YTD, верни его тоже.\n"
        "6. **Одна таблица может раскрывать несколько целевых показателей.** "
        "Не останавливайся после первой найденной строки: проверь остальные строки "
        "той же таблицы на другие метрики из списка.\n"
        "7. **Компоненты и итоги.** Если описание метрики требует сумму нескольких "
        "явно раскрытых компонент, используй итоговую строку или все перечисленные "
        "компоненты; не подменяй сумму первым похожим компонентом. Неподписанная "
        "subtotal-строка между total costs и operating expenses в P&L может быть "
        "gross profit, если арифметически совпадает с revenue плюс signed costs.\n"
        "8. **Знаки.** Прибыль/доходы — обычно положительное. Убыток — отрицательное "
        "или со скобками. Расходы и резервы — записаны со скобками = негатив. Просто "
        "оставляй как написано в источнике (со скобками если есть), парсер обработает.\n\n"
        "Возвращай ровно один валидный JSON-объект. Никаких пояснений, markdown-обёрток, "
        "комментариев — только JSON."
    )


def build_legacy_ru_user_prompt(
    *,
    pdf_text_for_prompt: str,
    period_current: str,
    period_prev: str,
    periods_detected: list[str],
    info_blocks: list[str],
    page_instructions: str,
    schema_lines: list[str],
    include_non_financial_industry_note: bool = False,
) -> str:
    """Russian NF/IFRS user prompt — exactly what legacy built inline."""
    periods_line = (
        ", ".join(periods_detected)
        if periods_detected
        else "определи из заголовков таблиц"
    )
    return (
        f"# Полный текст отчёта\n\n{pdf_text_for_prompt}\n\n"
        f"# Информация о показателях для извлечения\n\n"
        f"Основной период для совместимости = **{period_current}**, сравнительный = **{period_prev}**.\n"
        f"Ориентиры по периодам, найденные Python до извлечения: {periods_line}.\n"
        "Для каждого показателя верни все явно раскрытые периоды в поле `values`; ключ периода "
        "оставляй буквальным текстом из шапки таблицы, чтобы Python нормализовал его после ответа.\n\n"
        + (
            _non_financial_industry_alias_note_ru() + "\n\n"
            if include_non_financial_industry_note
            else ""
        )
        + "\n\n".join(info_blocks)
        + "\n\n# Алгоритм работы (выполняй мысленно перед каждой метрикой)\n"
        "1. Прочитай 'Где искать' и найди соответствующий раздел в отчёте.\n"
        "2. Найди строку, заголовок которой совпадает с названием метрики или одним "
        "из синонимов.\n"
        "3. Сверь содержание с описанием — ту ли метрику ты нашёл.\n"
        "4. Возьми числовые значения для всех периодов, которые есть в этой строке/таблице, "
        "включая сравнительные столбцы прошлого года/квартала/YTD.\n"
        "5. Проверь, не содержит ли эта же таблица строки для других показателей из списка.\n"
        "6. Сохрани ключ периода как в шапке таблицы. Не сокращай полный заголовок до FY/Q/6M/9M/даты.\n"
        "7. Если строки нет — `values: {}` или значения периода = `null`.\n"
        "8. Если есть сомнения, что это именно та метрика — `null`."
        + page_instructions
        + "\n\n# Формат ответа\n\nJSON-объект, ключи строго совпадают со списком ниже:\n\n{\n  "
        + ",\n  ".join(schema_lines)
        + "\n}"
    )


def build_legacy_en_system_prompt(reporting_standard: str) -> str:
    """English mirror of the russian NF/US-GAAP system prompt.

    Phase D-5: replaces the legacy "english report gets russian prompt"
    quirk for the two us_gaap_*_en cells. Structurally parallel to the
    RU version — same rule order, same headers, just translated.
    """
    return (
        financial_analyst_intro_en(reporting_standard)
        + "\n\n"
        + verbatim_value_rules_en()
        + "\n\n"
        + period_rules_en()
        + "\n\n"
        + actual_values_only_rules_en()
        + "\n\n"
        "## Additional extraction rules\n"
        "1. **Better null than a wrong number.** If you are not 100% sure "
        "you found the right metric, return null. A false value is worse "
        "than a miss.\n"
        "2. **For main metrics, prefer the primary financial statements.** "
        "Use the Statement of operations, balance sheet and cash flows (or "
        "their IFRS equivalents) first. Use MD&A, segment tables and non-GAAP "
        "reconciliations only when the primary form does not disclose the "
        "metric directly. If the same line appears in the primary form with "
        "parentheses / a minus sign and in MD&A as a positive number, take "
        "the signed value from the primary form.\n"
        "3. **First locate the section of the report where the metric lives.** "
        "Use the 'Where to look' field for each metric.\n"
        "4. **Match row headers against the metric description.** Do not "
        "confuse similar lines: \"Cost of revenue\" ≠ \"Operating expenses\"; "
        "\"Financial income\" ≠ \"Interest income (banks)\".\n"
        "5. **Return every period the row/table discloses for the metric.** "
        "Do not stop at two columns or at the primary/prior labels above. "
        "If the row has comparative prior-year/quarter/YTD columns, return them too.\n"
        "6. **One table can disclose several target metrics.** Do not stop "
        "after the first matching row: check the remaining rows of the same "
        "table against other metrics in the list.\n"
        "7. **Components vs totals.** If the metric description requires a "
        "sum of disclosed components, use the subtotal line or all listed "
        "components; do not substitute the sum with the first similar "
        "component. An unlabelled subtotal between total costs and operating "
        "expenses on the P&L can be gross profit if it arithmetically matches "
        "revenue plus signed costs.\n"
        "8. **Signs.** Profit / income — usually positive. Loss — negative or "
        "in parentheses. Expenses and provisions — written in parentheses = "
        "negative. Leave the value as written in the source (with parentheses "
        "if present); the parser will handle it.\n\n"
        "Return exactly one valid JSON object. No explanations, no markdown "
        "wrappers, no comments — JSON only."
    )


def build_legacy_en_user_prompt(
    *,
    pdf_text_for_prompt: str,
    period_current: str,
    period_prev: str,
    periods_detected: list[str],
    info_blocks: list[str],
    page_instructions: str,
    schema_lines: list[str],
    include_non_financial_industry_note: bool = False,
) -> str:
    """English mirror of the russian user prompt. Same structure."""
    periods_line = (
        ", ".join(periods_detected)
        if periods_detected
        else "infer them from the table headers"
    )
    return (
        f"# Full report text\n\n{pdf_text_for_prompt}\n\n"
        f"# Metric extraction targets\n\n"
        f"Primary period for backward-compat = **{period_current}**, "
        f"prior comparative = **{period_prev}**.\n"
        f"Python pre-detected period hints: {periods_line}.\n"
        "For each metric return every explicitly disclosed period in the "
        "`values` field; keep the period key as the literal text from the "
        "table header so Python can normalize it after the response.\n\n"
        + (
            _non_financial_industry_alias_note_en() + "\n\n"
            if include_non_financial_industry_note
            else ""
        )
        + "\n\n".join(info_blocks)
        + "\n\n# Algorithm (run mentally for each metric)\n"
        "1. Read 'Where to look' and find the matching section in the report.\n"
        "2. Find the row whose header matches the metric name or one of its "
        "aliases.\n"
        "3. Cross-check the content against the description — is this really "
        "the same metric?\n"
        "4. Take numeric values for every period the row/table discloses, including "
        "comparative prior-year/quarter/YTD columns.\n"
        "5. Check whether the same table also discloses other target metrics.\n"
        "6. Keep the period key as it appears in the table header. Do not "
        "shorten a full header to FY/Q/6M/9M/date.\n"
        "7. If the row is missing — `values: {}` or value for the period = `null`.\n"
        "8. If you have any doubt this is the right metric — `null`."
        + page_instructions
        + "\n\n# Response format\n\nA JSON object whose keys exactly match the "
        "list below:\n\n{\n  "
        + ",\n  ".join(schema_lines)
        + "\n}"
    )


__all__ = [
    "build_legacy_ru_system_prompt",
    "build_legacy_ru_user_prompt",
    "build_legacy_en_system_prompt",
    "build_legacy_en_user_prompt",
]
