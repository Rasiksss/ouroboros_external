"""Каркас анализа не должен требовать того, чего нет в реестре расчётов (#137).

ЗАЧЕМ. Промпт анализа раньше прямо разрешал: «показатели, которых в таблице нет,
считай по отчёту сам». Рядом с числами, посчитанными Python, появлялись числа из
головы модели, и сверить их было нечем — ровно тот случай, когда таблица и вердикт
противоречат друг другу по одному отчёту.

Закрывается с двух сторон: недостающие коэффициенты заведены в реестр формулами
(этот файл проверяет, что они СЧИТАЮТСЯ), а из промпта убрано разрешение считать
самому (проверяется здесь же).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.skills.bulk_extractor import _calculate_metrics_pair
from finengine.skills.metric_schema import registry_for_profile

REPO = Path(__file__).resolve().parents[1]


def _calc_defs(profile: str) -> list:
    return list(registry_for_profile(profile)["calculated"])


def _calc_enums(profile: str) -> set[str]:
    """Реестр отдаёт расчёты кортежами (enum, name, formula, deps), а не словарями."""
    return {(c[0] if isinstance(c, tuple) else c["enum"]) for c in _calc_defs(profile)}


def _compute(profile: str, current: dict, prev: dict) -> dict:
    cur, _ = _calculate_metrics_pair(_calc_defs(profile), current, prev)
    return cur


# ─────────────────────── нефинансовые: чего требовал каркас ───────────────────────

NF_CURRENT = {
    "TOTAL_CURRENT_ASSETS": 1000.0, "INVENTORIES": 300.0,
    "TOTAL_CURRENT_LIABILITIES": 400.0,
    "NET_OPERATING_CASH_FLOW": 240.0,
    "OPERATING_PROFIT": 200.0, "DEPRECIATION_AMORTIZATION": 100.0,  # EBITDA = 300
}
NF_PREV = {**NF_CURRENT, "TOTAL_CURRENT_ASSETS": 900.0}


def test_quick_ratio_is_computed_by_code():
    """«Быстрая ликвидность» стоит в каркасе quick-анализа отдельной строкой."""
    got = _compute("ifrs_nf_ru", NF_CURRENT, NF_PREV)
    assert got["QUICK_RATIO"] == pytest.approx((1000 - 300) / 400)


def test_working_capital_is_computed_by_code():
    got = _compute("ifrs_nf_ru", NF_CURRENT, NF_PREV)
    assert got["WORKING_CAPITAL"] == pytest.approx(1000 - 400)


def test_ebitda_to_ocf_conversion_is_computed_by_code():
    """Конверсия EBITDA→OCF — требование раздела «Денежные потоки»."""
    got = _compute("ifrs_nf_ru", NF_CURRENT, NF_PREV)
    assert got["OCF_TO_EBITDA"] == pytest.approx(240 / 300 * 100)


# ─────────────────────────── банки: ROA и декомпозиция NIM ───────────────────────────

BANK_CURRENT = {
    "NET_INCOME": 150.0, "TOTAL_ASSETS": 3000.0,
    "INTEREST_INCOME_ER": 300.0,
    # Процентные расходы в разметке ОТРИЦАТЕЛЬНЫ (Сбер-2024: -4185.8).
    "INTEREST_EXPENSE_ER": -120.0,
    "TOTAL_LOANS_TO_CLIENTS": 1800.0, "SECURITIES_ASSETS": 400.0,
    "INTERBANK_DEPOSITS": 200.0,                      # процентные активы = 2400
    "CLIENT_DEPOSITS": 1500.0, "INTERBANK_LOANS": 200.0,
    "DEBT_SECURITIES_ISSUED": 100.0, "FUNDS_FROM_CENTRAL_BANK": 100.0,
    "JUNIOR_DEBT": 100.0,                             # процентные обязательства = 2000
}
BANK_PREV = dict(BANK_CURRENT)  # средние = текущим, чтобы проверять формулу, а не AVG


def test_bank_roa_is_computed_by_code():
    """ROA требуется разделом «Прибыльность», а среди банковских расчётов его не было.

    Ожидание В ПРОЦЕНТАХ (решение владельца 20.08): семейство доходностей
    считается процентом, а не долей.
    """
    got = _compute("ifrs_bank_ru", BANK_CURRENT, BANK_PREV)
    assert got["ROA"] == pytest.approx(150 / 3000 * 100)


def test_nim_decomposition_is_computed_by_code():
    """Каркас требует не просто NIM, а декомпозицию: доходность против стоимости."""
    got = _compute("ifrs_bank_ru", BANK_CURRENT, BANK_PREV)
    assert got["ASSET_YIELD"] == pytest.approx(300 / 2400 * 100)
    assert got["FUNDING_COST"] == pytest.approx(120 / 2000 * 100)


def test_funding_cost_is_positive_despite_negative_expense():
    """Стоимость фондирования — величина положительная, а расход в разметке со знаком минус."""
    got = _compute("ifrs_bank_ru", BANK_CURRENT, BANK_PREV)
    assert got["FUNDING_COST"] > 0


# ───────────────────────── промпт больше не разрешает считать в уме ─────────────────────────


def test_prompt_no_longer_licenses_mental_arithmetic():
    """Проверяем ГОТОВЫЙ промпт, а не исходник: важно, что видит модель.

    (Первая версия теста читала framework.py целиком и падала на собственном
    комментарии, где снятая фраза процитирована. Модели комментарии не видны —
    значит и проверять надо то, что до неё доезжает.)
    """
    from finengine.skills.analysis import framework as F  # noqa: PLC0415

    prompt = F.build_prompt(
        company_name="Тест", company_type="non_financial", standard="ifrs",
        depth="quick", ocr_text="===PAGE 1===\nОтчет о прибылях и убытках\n",
        metrics_table="SALES_REVENUE | Выручка | 100 | стр. 1\n",
    )
    assert "считай по отчёту сам" not in prompt, "разрешение считать в уме вернулось"
    assert "АРИФМЕТИКА В УМЕ ЗАПРЕЩЕНА" in prompt
    assert "нет данных" in prompt, (
        "нужен честный выход, иначе модель всё равно прикинет")
    # ⚠️ Запрет касается АРИФМЕТИКИ, а не чтения отчёта. Первая версия правки
    # (01.08) звучала как «нет в таблице — значит данных нет» и противоречила
    # главному правилу анализа: «все числа бери из текста отчёта». Так модель
    # перестала бы приводить разовые статьи, оговорки и нормативы из примечаний —
    # то есть ровно ту качественную часть, ради которой анализ читает документ.
    assert "ФАКТ ИЗ ТЕКСТА" in prompt, "чтение отчёта не должно попадать под запрет"
    # #238 переставил границу, но не снял её: ПОКАЗАТЕЛИ и КОЭФФИЦИЕНТЫ теперь
    # только из таблицы (раньше правило звучало «все числа бери из текста
    # отчёта» и прямо разрешало счёт по цитатам), а ВСЁ ОСТАЛЬНОЕ фактическое
    # по-прежнему читается из документа. Без второй половины анализ перестал бы
    # приводить разовые статьи, оговорки и нормативы из примечаний.
    assert "Показатели и коэффициенты бери ИЗ ТАБЛИЦЫ ЗАЗЕМЛЕНИЯ" in prompt, (
        "правило «числа из таблицы, а не из текста» пропало")
    assert "Всё остальное фактическое — из текста отчёта" in prompt, (
        "правило документо-ориентированного анализа обязано остаться")
    # Составитель анализа — ОДИН вызов модели без инструментов (run.py →
    # complete_with_usage). Отправлять его к калькулятору = вписать в промпт
    # ложь: дотянуться он не может и посчитает сам.
    assert "fe_calc" not in prompt, (
        "составителю анализа инструменты не переданы — звать его к калькулятору нельзя")


def test_every_cell_has_the_coefficients_its_framework_demands():
    """Реестры не должны разъезжаться между собой по этому набору."""
    for profile in ("ifrs_nf_ru", "ifrs_nf_en", "us_gaap_nf_en"):
        assert {"QUICK_RATIO", "WORKING_CAPITAL", "OCF_TO_EBITDA"} <= _calc_enums(profile), profile
    for profile in ("ifrs_bank_ru", "ifrs_bank_en", "us_gaap_bank_en"):
        assert {"ROA", "ASSET_YIELD", "FUNDING_COST"} <= _calc_enums(profile), profile
