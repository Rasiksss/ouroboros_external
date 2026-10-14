"""Отношение ЗАПАСА к ПОТОКУ приведено к длине периода (порт upstream 1d3e005).

ЦЕНА. Признак `annualize` (40 записей по восьми ячейкам) поднимает к году
показатели, где поток стоит в ЧИСЛИТЕЛЕ: ROE, ROA, NIM, стоимость риска.
Обратный случай — остаток на дату в числителе, поток за период в знаменателе —
не поднимал никто:

    Чистый долг / EBITDA        7,79x  ->  3,90x
    Долг / операционный поток  39,46x  -> 19,73x
    Дни дебиторки             161,38   ->  80,47
    Дни запасов               227,82   -> 113,60

Слева — полугодовой отчёт, посчитанный так, как считал наш движок до этой
правки: долг на 30 июня делился на EBITDA за ШЕСТЬ месяцев, а оборачиваемости
делили зашитые 365 дней на полугодовой поток. 7,79x — это долговая нагрузка
компании в беде, и это первое число, на которое смотрит кредитный читатель.

Числа взяты из полугодового отчёта Русагро — те же, на которых upstream ловил
дефект, чтобы правку можно было сверить с их глазами.

РАСХОЖДЕНИЕ С АПСТРИМОМ, СДЕЛАННОЕ СОЗНАТЕЛЬНО. `DAYS_IN_PERIOD` у нас и у них
совпадает (182 / 91 / 274), а `ANNUAL()` — нет: они умножают на 365/дней, мы на
12/месяцев. На полугодии это 2,0055 против ровно 2 — 0,3% разницы, поэтому
чистый долг у нас 3,90x, а в их сообщении 3,89x. Взяли месяцы, потому что
приведение EBITDA к году — конвенция «за полугодие умножь на два», а не
посуточное начисление, и потому что подпись обязана быть ТОЧНОЙ дробью,
которую читатель воспроизведёт (12/6, не 365/182 при 181 дне в первом полугодии).

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (проделана, каждая мутация покраснела):
  1. `months = 12` вместо `_period_months(...)` в `formula_for_period` →
     6 красных, включая обе подписи;
  2. `days_in_period` всегда возвращает год → 6 красных;
  3. `trace["formula"] = formula` вместо раскрытой → красные ровно две подписи;
  4. множитель `f"{12 / months:.2f}"` вместо дроби «12/9» → 3 красных
     (округление сдвигает и само ЧИСЛО девятимесячной нагрузки, не только текст);
  5. откат одной формулы реестра к `NET_DEBT / EBITDA` →
     `test_stock_to_flow_ratios_stayed_normalised_in_every_cell`;
  6. `"annualize": true` на формуле с `ANNUAL()` → `test_no_metric_is_annualised_twice`;
  7. снять свои скобки у аргумента — `f"({inner} * 12/{months})"` вместо
     `f"(({inner}) * 12/{months})"` → 4 красных, и среди них ЗНАЧЕНИЕ
     `test_a_compound_argument_is_annualised_whole` (10 вместо 2,5), а не
     только текст подписи.
"""
from __future__ import annotations

import re

import pytest

from finengine.skills.bulk_extractor.formula_engine import (
    _calculate_metrics_for_periods,
    days_in_period,
    formula_for_period,
)
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile


def _registry_rows(profile_id: str) -> list[dict]:
    registry = registry_for_profile(profile_id)
    return [
        {
            "enum": enum,
            "formula": formula,
            **registry["calculated_meta"].get(enum, {}),
        }
        for enum, _name, formula, _deps in registry["calculated"]
    ]

LEVERAGE = [("RATIO_NET_DEBT_EBITDA", "", "NET_DEBT / ANNUAL(EBITDA)", [])]
DAYS = [(
    "ACCOUNTS_RECEIVABLE_TURNOVER", "",
    "DAYS_IN_PERIOD / (SALES_REVENUE / AVG (ACCOUNTS_RECEIVABLE))", [],
)]

NET_DEBT = 137817.98
EBITDA_6M = 17680.721
REVENUE_6M = 167774.391
RECEIVABLES = 74180.0


def _leverage(period: str) -> tuple[float, dict]:
    trace: dict = {}
    result = _calculate_metrics_for_periods(
        LEVERAGE,
        {period: {"NET_DEBT": NET_DEBT, "EBITDA": EBITDA_6M}},
        [period],
        trace_by_period=trace,
    )
    return result[period]["RATIO_NET_DEBT_EBITDA"], trace[period]["RATIO_NET_DEBT_EBITDA"]


def _days_outstanding(duration_label: str, closing_date: str) -> tuple[float, dict]:
    # AVG читает балансовые ДАТЫ, поэтому точечные строки обязаны быть на месте
    # и стоять на той дате, которой период закрывается.
    by_period = {
        duration_label: {"SALES_REVENUE": REVENUE_6M},
        closing_date: {"ACCOUNTS_RECEIVABLE": RECEIVABLES},
        "31.12.2024": {"ACCOUNTS_RECEIVABLE": RECEIVABLES},
    }
    trace: dict = {}
    result = _calculate_metrics_for_periods(
        DAYS, by_period, list(by_period), trace_by_period=trace
    )
    enum = "ACCOUNTS_RECEIVABLE_TURNOVER"
    return result[duration_label][enum], trace[duration_label][enum]


# ── длина периода читается из метки ──────────────────────────────────────────

@pytest.mark.parametrize(("label", "days"), [
    ("FY 2025", 365),
    ("12M 2025", 365),
    ("9M 2025", 274),
    ("6M 2025", 182),
    ("3M 2025", 91),
    ("Q2 2025", 91),
    ("31.12.2025", 365),  # у даты своей длительности нет
    ("", 365),
])
def test_period_length_is_read_from_the_label(label: str, days: int) -> None:
    assert days_in_period(label) == days


# ── ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ: во сколько раз отличаемся от наивного деления ────

@pytest.mark.parametrize(("label", "periods_in_year"), [
    ("6M 2025", 2),
    ("3M 2025", 4),
    ("Q2 2025", 4),
    ("9M 2025", 12 / 9),
])
def test_leverage_differs_from_naive_division_by_periods_in_a_year(
    label: str, periods_in_year: float,
) -> None:
    """Главная проверка: не «число изменилось», а «изменилось РОВНО во столько».

    Наивное деление — то, что движок печатал до правки, и то, что читатель
    получит, если поделит вход на вход. Расхождение обязано быть ровно числом
    периодов в году, иначе приведение сделано по чужому правилу.
    """
    naive = NET_DEBT / EBITDA_6M
    value, _trace = _leverage(label)
    assert naive / value == pytest.approx(periods_in_year, rel=1e-9)


def test_annual_report_is_untouched() -> None:
    """ANNUAL() за двенадцать месяцев — тождество: годовой отчёт не двигается."""
    value, _trace = _leverage("FY 2025")
    assert value == pytest.approx(NET_DEBT / EBITDA_6M, rel=1e-12)


def test_days_outstanding_uses_the_reports_own_length() -> None:
    """365 дней против полугодовой выручки удваивали каждую оборачиваемость."""
    half, _trace = _days_outstanding("6M 2025", "30.06.2025")
    year, _trace_year = _days_outstanding("FY 2025", "31.12.2025")
    assert half == pytest.approx(80.47, abs=0.01)
    assert year == pytest.approx(161.38, abs=0.01)
    assert year / half == pytest.approx(365 / 182, rel=1e-9)


# ── ПОДПИСЬ: читатель обязан получить напечатанное число ─────────────────────

def test_signature_of_an_interim_leverage_reproduces_the_printed_number() -> None:
    """`ANNUAL(EBITDA)` читателю не сходится — сходится `((EBITDA) * 12/6)`.

    Без множителя в подписи аналитик делит вход на вход, получает 7,79 против
    напечатанных 3,89 и читает это как ошибку расчёта.
    """
    value, trace = _leverage("6M 2025")
    assert trace["formula"] == "NET_DEBT / ((EBITDA) * 12/6)"
    by_enum = {i["enum"]: i["value"] for i in trace["inputs"]}
    assert by_enum["NET_DEBT"] / (by_enum["EBITDA"] * 12 / 6) == pytest.approx(value)


def test_signature_of_days_outstanding_names_the_day_count() -> None:
    """`DAYS_IN_PERIOD` — имя времени расчёта, в подписи обязано стать числом."""
    value, trace = _days_outstanding("6M 2025", "30.06.2025")
    assert trace["formula"] == "182 / (SALES_REVENUE / AVG (ACCOUNTS_RECEIVABLE))"
    by_enum = {i["enum"]: i["value"] for i in trace["inputs"]}
    assert 182 / (by_enum["SALES_REVENUE"] / by_enum["ACCOUNTS_RECEIVABLE"]) == pytest.approx(value)


def test_the_multiplier_is_written_as_an_exact_fraction() -> None:
    """«12/9» — это 1,3333…, и напечатанное округление не даёт результата."""
    assert formula_for_period("X / ANNUAL(Y)", "9M 2025") == "X / ((Y) * 12/9)"


# ── СОСТАВНОЙ АРГУМЕНТ: к году поднимается весь поток, а не хвост суммы ──────

COMPOUND = [("X", "", "DEBT / ANNUAL(EBITDA - ONE_OFF)", [])]
COMPOUND_INPUTS = {"DEBT": 100.0, "EBITDA": 30.0, "ONE_OFF": 10.0}


def _compound(period: str) -> tuple[float, dict]:
    trace: dict = {}
    result = _calculate_metrics_for_periods(
        COMPOUND, {period: dict(COMPOUND_INPUTS)}, [period], trace_by_period=trace,
    )
    return result[period]["X"], trace[period]["X"]


def test_a_compound_argument_is_annualised_whole() -> None:
    """Множитель приписывается СТРОКОЙ, и без своих скобок у аргумента его
    забирает последнее слагаемое: `EBITDA - ONE_OFF * 12/6` — это не приведение
    разности, а вычитание удвоенного вычитаемого. 100 / (30 - 10*2) = 10 вместо
    100 / ((30 - 10)*2) = 2,5, то есть вчетверо мимо, и подпись такой промах не
    ловит: в вычислитель едет та же строка, она сходится сама с собой.
    """
    value, _trace = _compound("6M 2025")
    naive = COMPOUND_INPUTS["DEBT"] / (COMPOUND_INPUTS["EBITDA"] - COMPOUND_INPUTS["ONE_OFF"])
    assert value == pytest.approx(naive / 2, rel=1e-12)
    assert value == pytest.approx(2.5, rel=1e-12)


def test_a_compound_argument_keeps_its_own_parentheses_in_the_signature() -> None:
    """Подпись обязана показать читателю ТУ ЖЕ группировку, что ушла в счёт."""
    _value, trace = _compound("6M 2025")
    assert trace["formula"] == "DEBT / ((EBITDA - ONE_OFF) * 12/6)"


def test_a_compound_argument_is_untouched_on_an_annual_report() -> None:
    """Год — тождество и здесь: скобки есть, множителя нет, число прежнее."""
    value, trace = _compound("FY 2025")
    assert trace["formula"] == "DEBT / (EBITDA - ONE_OFF)"
    assert value == pytest.approx(
        COMPOUND_INPUTS["DEBT"] / (COMPOUND_INPUTS["EBITDA"] - COMPOUND_INPUTS["ONE_OFF"]),
        rel=1e-12,
    )


def test_a_formula_without_the_new_names_is_left_byte_for_byte() -> None:
    """332 расчётных строки реестра из 361 новых имён не содержат — их подпись
    не должна дрогнуть ни на байт."""
    formula = "NET_INCOME / AVG (TOTAL_EQUITY)"
    assert formula_for_period(formula, "6M 2025") is formula


# ── правило не должно уехать: реестр и признак `annualize` не пересекаются ───

def test_no_metric_is_annualised_twice() -> None:
    """`annualize` поднимает к году ПРИ ВЫДАЧЕ, `ANNUAL()` — внутри расчёта.

    Показатель, отмеченный обоими, уехал бы к году дважды: на полугодии — в
    четыре раза. Пересечение пустое, и это должно таким остаться.
    """
    offenders = [
        f"{profile_id}.json:{row['enum']}"
        for profile_id in PROFILE_IDS
        for row in _registry_rows(profile_id)
        if row.get("annualize")
        and ("ANNUAL(" in str(row.get("formula") or "")
             or "DAYS_IN_PERIOD" in str(row.get("formula") or ""))
    ]
    assert not offenders, f"приведение к году сделано дважды: {offenders}"


def test_stock_to_flow_ratios_stayed_normalised_in_every_cell() -> None:
    """Формулы реестра не должны молча вернуться к делению на сырой поток.

    ⚠️ ПЕРЕПИСАН 21.08.2026 (ревизия набора). До этого дня тест сверял формулу
    БАЙТ В БАЙТ с записанной здесь строкой — то есть охранял НАПИСАНИЕ.
    Замер: `NET_DEBT / ANNUAL( EBITDA )` движок раскрывает в тот же
    `NET_DEBT / ((EBITDA) * 12/6)`, а тест краснел (ложная тревога на пробеле);
    в обратную сторону дыр не нашлось, но и первой хватает.

    Теперь главное утверждение ИСПОЛНЯЕТСЯ: формула каждой такой записи
    прогоняется через `formula_for_period` и обязана ЗАВИСЕТЬ от длины
    периода — раскрытие на полугодии отличается и от самой формулы, и от
    раскрытия на годовом отчёте. Сверка состава входов осталась, но по
    строке, приведённой к одному пробелу.
    """
    expected = {
        "RATIO_NET_DEBT_EBITDA": "NET_DEBT / ANNUAL(EBITDA)",
        "RATIO_TOTAL_DEBT_EBITDA": "TOTAL_DEBT / ANNUAL(EBITDA)",
        "RATIO_TOTAL_DEBT_NOCF": "TOTAL_DEBT / ANNUAL(NET_OPERATING_CASH_FLOW)",
        "ACCOUNTS_PAYABLE_TURNOVER": "DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (ACCOUNTS_PAYABLE))",
        "ACCOUNTS_RECEIVABLE_TURNOVER": "DAYS_IN_PERIOD / (SALES_REVENUE / AVG (ACCOUNTS_RECEIVABLE))",
        "INVENTORIES_TURNOVER": "DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (INVENTORIES))",
    }

    def flat(text: str) -> str:
        """Формула без пробельной разницы: `ANNUAL( X )` и `ANNUAL(X)` — одно.

        Движок разбирает и то, и другое одинаково (см. `_SPELLINGS` в
        `test_two_annualizations_are_refused.py`), поэтому расходиться на
        пробеле сторож не должен.
        """
        return re.sub(r"\s*([()])\s*", r"\1", " ".join(text.split()))

    seen = 0
    for profile_id in PROFILE_IDS:
        for row in _registry_rows(profile_id):
            want = expected.get(str(row.get("enum")))
            if want is None:
                continue
            seen += 1
            formula = str(row["formula"])
            where = f"{profile_id}.json:{row['enum']}"

            half = formula_for_period(formula, "6M 2025")
            year = formula_for_period(formula, "FY 2025")
            assert half != formula, (
                f"{where}: движок оставил формулу «{formula}» как есть на "
                "полугодовом отчёте — запас поделён на поток за ПОЛГОДА. "
                "Это те самые 7,79x долговой нагрузки вместо 3,90x")
            assert half != year, (
                f"{where}: раскрытие «{half}» одинаково на полугодии и на годе "
                "— длина периода в формуле есть, а влияния не оказывает")

            assert flat(formula) == flat(want), f"{where}: {formula!r}"
    assert seen == 29, f"ожидали 29 записей в пяти нефинансовых ячейках, нашли {seen}"
