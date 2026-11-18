"""Куски промпта извлечения: кто ты, как читать периоды, как отдавать значения.

Текст, а не логика. Правится без понимания кода — это и есть цель переноса
данных из питона в правимые места.
"""
from __future__ import annotations



def _financial_analyst_intro_ru(reporting_standard: str) -> str:
    return (
        "Ты опытный финансовый аналитик. Извлекаешь значения метрик из финансового "
        "отчёта стандарта {reporting_standard}.\n"
        "Ты аккуратно сопоставляешь строки отчёта с описаниями, синонимами и подсказками "
        "по каждой метрике. Если строка похожа на нужный показатель, но не совпадает с "
        "его смыслом или включает лишние компоненты, возвращай null: ложное значение "
        "хуже пропуска."
    ).format(reporting_standard=reporting_standard)

def _financial_analyst_intro_en(standard: str) -> str:
    return (
        "You are a senior financial analyst extracting business breakdowns from "
        "{standard} financial reports. Match each row against the target metric "
        "description, aliases and hint. If a row is similar but not the requested "
        "metric, or includes extra components, return nothing for it: a false value "
        "is worse than a miss. Return only values explicitly present in segment, "
        "geography or product tables. Do not invent allocations."
    ).format(standard=standard)

def _period_rules_ru() -> str:
    return (
        "## Правила периодов\n"
        "Лучше возвращай ключ периода буквально как в шапке таблицы (`Year ended "
        "January 31, 2013`, `За год, закончившийся 31 декабря 2024`). Python потом "
        "нормализует. Если нормализуешь сам, используй: дата `5.11.2025`; `FY 2025` "
        "ТОЛЬКО для года/12 месяцев, закончившихся 31.12.2025; сдвинутый fiscal year "
        "— `12M 31.01.2025`; календарные кварталы `Q1 2025`..`Q4 2025`; YTD "
        "`6M 2025`/`9M 2025`; нестандартный период `3M 21.02.2025`. Не называй "
        "fiscal year, закончившийся не 31.12, как FY."
    )

def _period_rules_en() -> str:
    return (
        "Return period keys as literally as possible from the table header; the "
        "Python post-processor will normalize them. If you choose to normalize, "
        "use only these labels:\n"
        "- point-in-time date: 5.11.2025\n"
        "- FY 2025 ONLY for the 12 months/year ended 31.12.2025\n"
        "- shifted fiscal year / any annual period not ended 31.12: 12M 28.02.2025\n"
        "- calendar quarters: Q1 2025, Q2 2025, Q3 2025, Q4 2025\n"
        "- year-to-date calendar periods: 6M 2025, 9M 2025; first semester / "
        "H1 / first half of 2025 is 6M 2025\n"
        "- shifted/rolling interim periods: 3M 21.02.2025, 9M 28.02.2026, etc.\n"
        "Never label a fiscal year ended January/February/September/etc. as FY."
    )

def _verbatim_value_rules_ru() -> str:
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
        "Если строка нужной метрики найдена, но ячейка периода содержит только "
        "прочерк (`-`, `—`, `–`, `−`) или пустое значение, верни строку \"0\" "
        "для этого периода: это означает отсутствие движения/суммы в найденной "
        "строке, а не отсутствие метрики. Если самой строки/показателя в отчёте "
        "нет — используй null или пустой `values`."
    )

def _verbatim_value_rules_en() -> str:
    return (
        "Return each value exactly as it appears in the table cell. Do NOT convert "
        "units, do NOT normalize thousands separators, do NOT remove commas, spaces "
        'or parentheses. If the table says "25,785", return "25,785", not "25785" '
        'and not "25.785". If the table says "(4,190)", return "(4,190)"; '
        'parentheses are mandatory: "(465)" must stay "(465)", not "465". '
        "If the requested metric row is present but a period cell is blank or "
        "contains only -, —, – or −, return \"0\" for that period. Use null only "
        "when the metric row itself is absent or the value is not disclosed. "
        "Put the table/header unit literally into unit_text, for example "
        '"in millions" or "$ in millions".'
    )

def _actual_values_only_rules_ru() -> str:
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
        "за отчётный период."
    )

def _actual_values_only_rules_en() -> str:
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
        "reported in the statements or notes."
    )
