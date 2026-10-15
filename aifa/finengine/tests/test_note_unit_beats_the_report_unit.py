"""Показатель, раскрытый в примечании, объявляет СВОЮ единицу.

ЦЕНА (прогоны владельца по Северстали Q1-2025, 19–20.08.2026). Отчёт свёрстан в
миллионах рублей, а примечание называет свой масштаб: «договорные обязательства
по закупкам капитального характера на сумму 84,7 млрд рублей», «производные
финансовые инструменты … в сумме 5,3 млрд рублей». Единицу показателя движок
берёт из одного поля `unit_text`, которое модель заполняет по указанию «из
заголовка таблицы / шапки колонки / шапки документа». Фраза примечания в этом
списке не значилась — модели некуда было объявить «млрд», и она подставляла
шапку документа. Число уменьшалось в тысячу раз.

ЦЕНА ВТОРАЯ, ДЕТЕРМИНИРОВАННАЯ. Ярлык показателя берётся у ПЕРВОЙ строки
периода — а `_normalise_money_value` без числа возвращает единицу как пришла.
Раскрытие только за сравнительную дату («на 31 декабря 2024 года: 105,1 млрд»)
отдавало строку, где число приведено к миллионам, а колонка «Единица» в выгрузке
говорит «млрд руб.»: ×1000 враньём на глазах у пользователя.

СКОЛЬКО ТАКИХ СТРОК В РАЗМЕТКЕ: 56 на 13 документов из 112, и ВСЕ РОВНО в
тысячу раз (`unit` строки не совпадает с отчётной единицей документа). Показатели:
COMMITTED_CREDIT_FACILITIES 12, GUARANTEES 6, INDIVIDUALS_DEPOSITS 5,
CORPORATE_DEPOSITS 5, SALES_REVENUE 4, DOUBTFUL_ACCOUNTS_ALLOWANCE 4,
LOANS_STAGE1/2/3_GROSS по 3, DERIVATIVE_LIABILITIES 3, лизинговые обязательства
по 2, FUTURE_CAPEX_LIABILITIES 2, RISK_WEIGHTED_ASSETS 2.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана, каждая мутация покраснела):
  1. убрать абзац про фразу примечания из `_verbatim_value_rules` (ru) — падает
     `test_russian_prompt_sends_the_model_to_the_sentence_for_the_unit`;
  2. то же в английской ветке — падает
     `test_english_prompt_sends_the_model_to_the_sentence_for_the_unit`;
  3. снять ветку `if v is None` в `_coerce_period_values` — падает
     `test_an_empty_cell_does_not_relabel_the_metric`.
"""
from __future__ import annotations

from finengine.skills.bulk_extractor import _coerce_period_values
from finengine.skills.finengine2_prompts import (
    build_extraction_prompt,
    prompt_profile_for_cell,
)

# Северсталь Q1-2025: отчёт в миллионах рублей.
REPORT = dict(period_current="31.03.2025", period_prev="31.12.2024", default_unit="млн. руб.")


def _prompt(cell: str) -> str:
    """Собираем ТОТ промпт, который правило и везёт, — извлечения, по профилю
    ячейки. Первая редакция сторожа смотрела в системный промпт и была зелёной
    при правиле, до модели не доезжающем."""
    return build_extraction_prompt(
        prompt_profile_for_cell(cell),
        target_enums=["FUTURE_CAPEX_LIABILITIES"],
        metric_blocks_json="[]",
        schema_lines=['"FUTURE_CAPEX_LIABILITIES": {}'],
        split_request=True,
        company_type="non_financial",
    )


def test_russian_prompt_sends_the_model_to_the_sentence_for_the_unit():
    """Проверяем СОБРАННЫЙ промпт, а не наличие строки в модуле: правило,
    не доехавшее до модели, работы не делает."""
    text = _prompt("ifrs_nf_ru")
    assert "НЕ ИЗ ТАБЛИЦЫ" in text
    assert "84,7 млрд рублей" in text


def test_english_prompt_sends_the_model_to_the_sentence_for_the_unit():
    text = _prompt("us_gaap_nf_en")
    assert "narrative sentence in a note" in text
    assert "thousandfold" in text


def test_note_scale_wins_over_the_report_scale():
    """Модель назвала единицу примечания — 84,7 млрд едут в отчётные миллионы."""
    pvs, _ = _coerce_period_values(
        {"values": {"31 марта 2025 года": "84,7"}, "unit_text": "млрд рублей"},
        enum="FUTURE_CAPEX_LIABILITIES",
        **REPORT,
    )
    assert [(p["period"], p["value"], p["unit"]) for p in pvs] == [
        ("31.03.2025", 84700.0, "млн. руб.")
    ]


def test_an_empty_cell_does_not_relabel_the_metric():
    """Раскрытие только за сравнительную дату. У пустой клетки числа нет, но
    ярлык её строки обязан совпасть с ярлыком строки, где число есть, — иначе
    единицу показателя выберет пустая клетка."""
    pvs, _ = _coerce_period_values(
        {
            "values": {"31 марта 2025 года": None, "31 декабря 2024 года": "105,1"},
            "unit_text": "млрд рублей",
        },
        enum="FUTURE_CAPEX_LIABILITIES",
        **REPORT,
    )
    assert {p["unit"] for p in pvs} == {"млн. руб."}
    # Именно эту строку оба движка берут как единицу показателя.
    assert pvs[0]["unit"] == "млн. руб."
    assert pvs[0]["value"] is None
