"""AIFA's extraction prompt templates, copied verbatim from
`Репа в репе/financier-extractor/src/financier_extractor/strategies/prompts/extract_from_table.py`
and `prompts/base.py`.

Only deliberate change vs upstream: we feed our own metric registry through
`get_parameters_metadata_description` (the upstream version reads from the
team's ParameterEnum + metadata helper). Behaviorally equivalent — same
fields (name_rus, additional_names, description, comment).
"""
from __future__ import annotations

EXPECTED_PARAMETERS_JSON_FORMAT = """
Выведи результат в формате JSON:
```json
{
    "periods": ["2023", ...],
    "parameters": [
        {
            "period": "2023",
            "extracted": [
                {
                    "type": "Выручка",
                    "value": "115000",
                    "unit": "млн. руб.",
                },
                ...
            ]
        },
        ...
    ]
}
```
"""

EXPECTED_FORMAT_CONSTRAINTS = """
- Значения ("value") должны быть числами без пробелов и запятых (например, "6 143 млн руб" → "6143").
- Единица измерения ("unit") должна быть явно указана. Если не указана в таблице — использовать по умолчанию "млн. руб.".
- Если значение отсутствует, укажи null.
- Период ("period" и массив "periods") — только год (например, "2023") или дата (например, "31 декабря 2023").
- Для каждого параметра указывай его название в поле "parameters".
"""

EXTRA_GUIDANCE = """
You are a highly advanced expert in extracting financial data from Markdown tables.

Lineage (type):
- Imitation — retain explicit JSON schema and structured reasoning steps.
- Avoidance — eliminate redundancy and ambiguity in instructions, particularly for value cleaning rules.
- Exploration — introduce new objective criteria for table selection (column header matching).
"""

PARAMETERS_DESCRIPTION_TEMPLATE = """Информация о показателях:
{parameters_description}"""

TABLES_PARAMETERS_EXTRACTION_TEMPLATE = """{extra_guidance}

Извлеки показатели: {parameter_titles} и периоды из таблиц ниже. Ниже представлены кандидаты таблиц, в которых с большой вероятностью находятся упомянутые показатели, поэтому показатели могут быть не представлены во всех вариантах.

{metadata_description}

{expected_format_constraints}

{tables_str}

{expected_format_description}
Прежде чем выбрать таблицу, проверь каждую таблицу на наличие числовых значений и соответствие заголовков столбцов указанным параметрам. Если заголовки столбцов не совпадают с параметрами, игнорируй таблицу.

Шаг за шагом:
1. Проанализируй каждую таблицу.
2. Определи, содержит ли таблица числовые значения.
3. Проверь, соответствуют ли заголовки столбцов указанным параметрам.
4. Если таблица содержит числовые значения и заголовки столбцов соответствуют параметрам, используй её для извлечения данных.
5. Извлеки значения для каждого показателя и периода.
6. Выведи результат в формате JSON, строго следуя указанному формату.
"""

TABLE_FORMAT_TEMPLATE = """Таблица {idx}:
```markdown
{table_markdown}
```"""


def parameter_metadata_description(metric: dict) -> str:
    """Render a single registry entry in the upstream "Информация о
    показателях" format — name + alternatives + description + comment."""
    line = f"- {metric['name_ru']}"
    aliases = (metric.get("aliases_ru") or []) + (metric.get("aliases_en") or [])
    aliases = [a for a in aliases if a and a != "Нет"]
    if aliases:
        line += f" (также может называться: {', '.join(aliases)})"
    if metric.get("description"):
        line += f"\n  Описание: {metric['description']}"
    if metric.get("hint"):
        line += f"\n  Примечание: {metric['hint']}"
    return line


def build_tables_extraction_prompt(metric: dict, candidate_tables: list[str]) -> str:
    """Assemble TABLES_PARAMETERS_EXTRACTION_TEMPLATE for a single metric.

    Upstream supports batches of metrics per call; we go one-at-a-time so
    the model sees only the relevant top-3 tables for that metric.
    """
    parameter_title = f"'{metric['name_ru']}'"
    metadata = PARAMETERS_DESCRIPTION_TEMPLATE.format(
        parameters_description=parameter_metadata_description(metric)
    )
    tables_str = "\n".join(
        TABLE_FORMAT_TEMPLATE.format(idx=i + 1, table_markdown=t)
        for i, t in enumerate(candidate_tables)
    )
    return TABLES_PARAMETERS_EXTRACTION_TEMPLATE.format(
        extra_guidance=EXTRA_GUIDANCE,
        parameter_titles=parameter_title,
        metadata_description=metadata,
        expected_format_constraints=EXPECTED_FORMAT_CONSTRAINTS,
        tables_str=tables_str,
        expected_format_description=EXPECTED_PARAMETERS_JSON_FORMAT,
    ).strip()
