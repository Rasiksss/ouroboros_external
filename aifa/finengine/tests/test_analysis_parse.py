"""Юнит-тесты чистых функций разбора markdown-анализа в структуру карточки.

Цель: `finengine.skills.analysis.parse` — parse_report + хелперы (_split_sections,
_parse_kpi, _flag_from, _parse_two_part, _parse_list). Все функции чистые (только
`re`, без LLM/сети/FAISS), поэтому тест самодостаточен и детерминирован.

Ожидания сверены с живым поведением функций, а не выведены из чтения кода.
"""

from __future__ import annotations

from finengine.skills.analysis.parse import (
    _flag_from,
    _parse_kpi,
    _parse_list,
    _parse_two_part,
    _split_sections,
    parse_report,
)


# --------------------------------------------------------------------------- #
# _split_sections — нарезка по заголовкам, текст до первого заголовка отброшен  #
# --------------------------------------------------------------------------- #

def test_split_sections_discards_preamble_before_first_header() -> None:
    secs = _split_sections("вводный мусор до заголовка\n# Финансовый анализ — X\nтело")
    assert secs == [("Финансовый анализ — X", "тело")]


def test_split_sections_multiple_headers_and_bodies() -> None:
    md = "## Вердикт\nстрока1\n### Ключевые показатели\n| a | b |"
    secs = _split_sections(md)
    assert secs == [
        ("Вердикт", "строка1"),
        ("Ключевые показатели", "| a | b |"),
    ]


# --------------------------------------------------------------------------- #
# parse_report — роутинг заголовков в роли                                      #
# --------------------------------------------------------------------------- #

def test_routing_h1_financial_analysis_header_is_discarded() -> None:
    r = parse_report("# Финансовый анализ — Компания X\nвводный абзац")
    # H1-заголовок документа не попадает ни в verdict, ни в sections.
    assert r["sections"] == []
    assert r["verdict"] == ""


def test_flag_from_risk() -> None:
    assert _flag_from("−12% / **Риск** (падение)") == ("risk", "падение")


def test_flag_from_warn() -> None:
    assert _flag_from("0% / **Зона внимания** (стагнация)") == ("warn", "стагнация")


def test_flag_from_ok() -> None:
    assert _flag_from("+4,6% / **Норма** (рост выручки)") == ("ok", "рост выручки")


def test_flag_from_na_no_data() -> None:
    assert _flag_from("н/д / нет данных") == ("na", "")


def test_flag_from_na_not_disclosed() -> None:
    assert _flag_from("не раскрыто") == ("na", "")


def test_flag_from_defaults_to_na_when_no_marker() -> None:
    assert _flag_from("просто текст") == ("na", "")


def test_flag_from_why_only_from_trailing_parens() -> None:
    # Скобки не в самом хвосте не считаются пояснением.
    flag, why = _flag_from("норма (в середине) хвост")
    assert flag == "ok"
    assert why == ""


# --------------------------------------------------------------------------- #
# _parse_kpi — разбор markdown-таблицы показателей                              #
# --------------------------------------------------------------------------- #

def test_parse_kpi_four_columns() -> None:
    body = (
        "| Показатель | Текущий | Предыдущий | Δ%/оценка |\n"
        "|---|---|---|---|\n"
        "| **Выручка** | 100 | 90 | +11,1% / **Норма** (рост) |\n"
        "| Чистая прибыль | 10 | 20 | −50% / **Риск** (падение) |\n"
    )
    rows = _parse_kpi(body)
    assert rows == [
        {"name": "Выручка", "value": "100", "prev": "90",
         "delta": "+11,1%", "flag": "ok", "why": "рост"},
        {"name": "Чистая прибыль", "value": "10", "prev": "20",
         "delta": "−50%", "flag": "risk", "why": "падение"},
    ]


def test_parse_kpi_slices_separator_and_header_rows() -> None:
    # Строка-разделитель |---| и строка-заголовок «Показатель» отброшены —
    # остаются только две строки данных.
    body = (
        "| Показатель | Текущий | Предыдущий | Δ%/оценка |\n"
        "|---|---|---|---|\n"
        "| Выручка | 100 | 90 | +11% / Норма |\n"
        "| Долг | 500 | 400 | +25% / Риск |\n"
    )
    rows = _parse_kpi(body)
    assert [r["name"] for r in rows] == ["Выручка", "Долг"]


def test_parse_kpi_delta_is_part_before_slash() -> None:
    body = "| Выручка | 100 | 90 | +11,1% / **Норма** (рост) |"
    assert _parse_kpi(body)[0]["delta"] == "+11,1%"


def test_parse_kpi_strips_bold_markers_in_name() -> None:
    body = "| **EBITDA** | 50 | 40 | +25% / Норма |"
    assert _parse_kpi(body)[0]["name"] == "EBITDA"


def test_parse_kpi_ignores_non_table_lines() -> None:
    body = "просто текст\n| Выручка | 100 | 90 | +11% / Норма |\nещё текст"
    rows = _parse_kpi(body)
    assert len(rows) == 1
    assert rows[0]["name"] == "Выручка"


def test_parse_kpi_tolerates_three_columns() -> None:
    # 3 колонки: показатель | значение | оценка. Значение читается, флаг/пояснение
    # выводятся из последней ячейки (она же становится prev — известная особенность).
    body = (
        "| Показатель | Значение | Оценка |\n"
        "|---|---|---|\n"
        "| Долг | 500 | Зона внимания (рост долга) |\n"
    )
    rows = _parse_kpi(body)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Долг"
    assert row["value"] == "500"
    assert row["flag"] == "warn"
    assert row["why"] == "рост долга"
    assert row["prev"] == "Зона внимания (рост долга)"


# --------------------------------------------------------------------------- #
# _parse_two_part — «Сильные стороны» / «Зоны внимания»                         #
# --------------------------------------------------------------------------- #

def test_parse_two_part_labels_on_separate_lines() -> None:
    body = (
        "**Сильные стороны**\n"
        "- рост выручки\n"
        "- низкий долг\n"
        "**Зоны внимания**\n"
        "- отрицательный OCF\n"
    )
    strengths, watch = _parse_two_part(body)
    assert strengths == ["рост выручки", "низкий долг"]
    assert watch == ["отрицательный OCF"]


def test_parse_two_part_inline_labels() -> None:
    body = (
        "- **Сильные стороны:** сильный баланс\n"
        "- **Зоны внимания:** волатильность цен\n"
    )
    strengths, watch = _parse_two_part(body)
    assert strengths == ["сильный баланс"]
    assert watch == ["волатильность цен"]


def test_parse_two_part_empty_body() -> None:
    assert _parse_two_part("") == ([], [])


# --------------------------------------------------------------------------- #
# _parse_list — нумерованный / маркированный список                             #
# --------------------------------------------------------------------------- #

def test_parse_list_numbered_and_bulleted() -> None:
    body = "1. первый\n2) второй\n- третий\n* четвёртый\nне пункт"
    assert _parse_list(body) == ["первый", "второй", "третий", "четвёртый"]


def test_parse_list_without_markers_is_empty() -> None:
    assert _parse_list("сплошной текст без списка") == []


def test_footnote_extracted_from_italic_revision_note() -> None:
    md = "## Вердикт\nтекст\n\n_Отчётность за 2023 пересмотрена аудитором._"
    assert parse_report(md)["footnote"] == "Отчётность за 2023 пересмотрена аудитором."


def test_footnote_extracted_from_blockquote_audit_note() -> None:
    md = "## Вердикт\nт\n\n> Отчетность прошла аудит и была пересмотрена"
    assert parse_report(md)["footnote"] == "Отчетность прошла аудит и была пересмотрена"


def test_footnote_absent_when_no_revision_note() -> None:
    assert parse_report("## Вердикт\nтекст без сноски")["footnote"] == ""


# --------------------------------------------------------------------------- #
# Толерантность — пустой markdown и проброс метаданных                          #
# --------------------------------------------------------------------------- #

def test_empty_markdown_yields_empty_fields_and_passes_metadata() -> None:
    r = parse_report(
        "",
        company="Сбер",
        period="2023",
        company_type="bank",
        standard="IFRS",
        language="ru",
    )
    # Метаданные пробрасываются как есть.
    assert r["company"] == "Сбер"
    assert r["period"] == "2023"
    assert r["company_type"] == "bank"
    assert r["standard"] == "IFRS"
    assert r["language"] == "ru"
    # Все разобранные поля пусты.
    assert r["verdict"] == ""
    assert r["kpi"] == []
    assert r["sections"] == []
    assert r["strengths"] == []
    assert r["watch"] == []
    assert r["conclusions"] == []
    assert r["footnote"] == ""


def test_none_markdown_is_tolerated() -> None:
    # markdown=None не должен падать (md = markdown or "").
    r = parse_report(None)  # type: ignore[arg-type]
    assert r["verdict"] == ""
    assert r["kpi"] == []


def test_metadata_defaults_are_empty_strings() -> None:
    r = parse_report("## Вердикт\nок")
    assert r["company"] == ""
    assert r["period"] == ""
    assert r["company_type"] == ""
    assert r["standard"] == ""
    assert r["language"] == ""
