"""Тесты вычислителя формул рассчитываемых показателей (`bulk_extractor`).

Вычислитель переехал с `eval(expr, {"__builtins__": {}})` на разбор в AST с белым
списком узлов. Эти тесты держат две границы:

1. РЕГРЕСС. `GOLDEN` — снимок ответов СТАРОГО eval-пути на всех формулах из
   десяти effective registry + `CALCULATED_NON_FINANCIAL` /
   `CALCULATED_BANK`, снятый на детерминированных синтетических входах
   (`_synthetic`). Числа в `GOLDEN` — `repr` float'а, сравнение точное: новый
   обход дерева повторяет порядок операций Python, поэтому расхождений в
   последнем бите быть не должно. Если формулу в реестре правят осознанно —
   пересчитать и обновить строку; если она «поехала» сама — это регресс.
   Метки периода здесь нет, а без неё `DAYS_IN_PERIOD` — те же 365, а
   `ANNUAL()` — тождество: приведение к длине периода годовой отчёт не двигает,
   и шесть переименованных формул стоят с ТЕМИ ЖЕ числами, что до порта.
2. БЕЗОПАСНОСТЬ. Побег через `__subclasses__`, доступ к атрибутам и вызов
   неразрешённой функции должны падать явной ошибкой, а не исполняться.
"""
from __future__ import annotations

import pytest

from finengine.skills import bulk_extractor as bx
from finengine.skills.metric_registry import CALCULATED_BANK, CALCULATED_NON_FINANCIAL
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile


def _synthetic(name: str) -> float:
    """Детерминированное значение входа: 1..97, никогда не ноль.

    Ноль исключён намеренно — иначе половина формул-долей упиралась бы в
    ZeroDivisionError и регресс проверял бы не арифметику, а обработку ошибок.
    """
    return float(1 + sum(ord(c) for c in name) % 97)


def _all_registry_formulas() -> list[str]:
    formulas: set[str] = set()
    for profile_id in PROFILE_IDS:
        for _enum, _name, formula, _deps in registry_for_profile(profile_id)["calculated"]:
            formulas.add(formula)
    for source in (CALCULATED_NON_FINANCIAL, CALCULATED_BANK):
        for row in source:
            formulas.add(row[2])
    return sorted(formulas)


# Снимок ответов старого eval-пути. Ключ — формула из реестра, значение — repr(float).
GOLDEN = {
    '(FREE_CASH_FLOW_DEBT + CAPEX + INTEREST_PAID)/ INTEREST_PAID': '3.5294117647058822',
    '(NET_INTEREST_INCOME + NET_COMMISSION_INCOME - TOTAL_CREDIT_ALLOWANCE - OPERATING_EXPENSES_BANKS) * 0,75': '48.0',
    '(SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES) / TOTAL_DEBT * 100%': '77.17391304347827',
    '(TOTAL_CURRENT_ASSETS - INVENTORIES) / TOTAL_CURRENT_LIABILITIES': '-0.40425531914893614',
    'DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (ACCOUNTS_PAYABLE))': '394.03409090909093',
    'DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (INVENTORIES))': '622.5738636363637',
    'DAYS_IN_PERIOD / (SALES_REVENUE / AVG (ACCOUNTS_RECEIVABLE))': '589.4749999999999',
    'ABS(CHANGE_IN_EXPECTED_CREDIT_LOSSES) / AVG(TOTAL_LOANS_TO_CLIENTS)': '20.526315789473685',
    '-CHANGE_IN_EXPECTED_CREDIT_LOSSES / AVG(TOTAL_LOANS_TO_CLIENTS) * 100%': '-2052.6315789473683',
    'NET_INCOME_SHAREHOLDERS / AVG (EQUITY_SHAREHOLDERS) * 100%': '90.90909090909092',
    'ABS(INTEREST_EXPENSE_ER) / AVG (INTEREST_BEARING_LIABILITIES) * 100%': '52.05320994794679',
    'ACCOUNTS_RECEIVABLE_TURNOVER + INVENTORIES_TURNOVER - ACCOUNTS_PAYABLE_TURNOVER': '61.0',
    'ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES': '56.0',
    'ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES - ASSET_SALES_PROCEEDS': '-37.0',
    'CAPEX / SALES_REVENUE * 100%': '197.5',
    'CASA_DEPOSITS / CLIENT_DEPOSITS * 100%': '26.31578947368421',
    'CASH_EQUIVALENTS + BANK_DEPOSITS': '97.0',
    'CASH_EQUIVALENTS + INTERBANK_DEPOSITS': '95.0',
    # Заведены 20.08.2026 вместе с формулами: складывает КОД, а не модель.
    'COMBINED_ASSET_PURCHASES - ASSET_SALES_PROCEEDS': '-1.0',
    'SECURITIES_FVPL + SECURITIES_FVOCI + SECURITIES_AMORTISED_COST': '99.0',
    'CLIENT_DEPOSITS + INTERBANK_LOANS + DEBT_SECURITIES_ISSUED + FUNDS_FROM_CENTRAL_BANK + JUNIOR_DEBT': '348.0',
    'CORPORATE_DEPOSITS + GOVERNMENTAL_DEPOSITS': '121.0',
    'CORPORATE_LOANS  / NON_CONSUMER_DEPOSITS * 100': '606.25',
    'DEMAND_DEPOSITS + SAVINGS_BANK_DEPOSITS': '154.0',
    'DIVIDENDS / NET_INCOME': '0.9340659340659341',
    'DIVIDENDS / NET_INCOME * 100%': '93.4065934065934',
    'DIVIDENDS_SHAREHOLDERS + DIVIDENDS_PAID_NONCONTROLLING_INTERESTS': '27.0',
    'EBITDA + CHANGES_OCF_ASSETS_LIABILITIES - TAX_EXPENDITURE - CAPEX - NET_INTEREST_PAID - DIVIDENDS + DIVIDENDS_RECEIVED': '-71.0',
    'EBITDA / INTEREST_PAID': '1.1176470588235294',
    'EBITDA / NET_INTEREST_PAID': '0.5507246376811594',
    'EBITDA / SALES_REVENUE * 100%': '95.0',
    'EBT - NET_INCOME': '-65.0',
    'ECL_ALLOWANCE_STAGE3 / LOANS_STAGE3_GROSS': '0.9428571428571428',
    'ECL_ALLOWANCE_STAGE3 / LOANS_STAGE3_GROSS * 100%': '94.28571428571428',
    'FEE_AND_COMMISSION_INCOME - FEE_AND_COMMISSION_EXPENSE': '4.0',
    'FREE_CASH_FLOW - INVESTMENTS - DIVIDENDS': '-161.0',
    'FREE_CASH_FLOW / INTEREST_PAID': '0.38235294117647056',
    'FREE_CASH_FLOW – INVESTMENTS – DIVIDENDS': '-161.0',
    'FREE_CASH_FLOW_GEN - INVESTMENTS - DIVIDENDS_SHAREHOLDERS - PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES': '-130.0',
    'FREE_CASH_FLOW_GEN / INTEREST_PAID': '1.0294117647058822',
    'FREE_CASH_FLOW_GEN – INVESTMENTS – DIVIDENDS_SHAREHOLDERS - PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES': '-130.0',
    'GROSS_NPA / TOTAL_GROSS_LOANS_TO_CLIENTS * 100%': '380.0',
    'GROSS_PROFIT / SALES_REVENUE * 100%': '222.5',
    # ⚠️ ШЕСТЬ СТРОК НИЖЕ ПЕРЕСНЯТЫ ОСОЗНАННО (#250, коммит e20303d — ячейки РСБУ
    # rsbu_nf_ru / rsbu_bank_ru). Значения не подогнаны под вычислитель: каждое
    # посчитано независимо, обычной арифметикой Python на тех же синтетических
    # входах, и только совпав с ответом вычислителя записано сюда.
    'INCOME_TAX': '97.0',
    'INDIVIDUALS_DEPOSITS / (INDIVIDUALS_DEPOSITS + CORPORATE_DEPOSITS)': '0.6666666666666666',
    'INDIVIDUALS_DEPOSITS / (INDIVIDUALS_DEPOSITS + CORPORATE_DEPOSITS) * 100%': '66.66666666666666',
    'INSURANCE_INCOME - INSURANCE_EXPENSE': '4.0',
    'INTEREST_EARNED_TOTAL - INTEREST_EXPENDED_TOTAL': '36.0',
    'INTEREST_INCOME_ER / AVG (INTEREST_EARNING_ASSETS) * 100%': '103.15789473684211',
    'INTEREST_PAID - INTEREST_RECEIVED': '-6.0',
    'INTEREST_PAID – INTEREST_RECEIVED': '-6.0',
    'LOANS_STAGE3_GROSS / TOTAL_LOANS_TO_CLIENTS': '17.5',
    'LOANS_STAGE3_GROSS / TOTAL_LOANS_TO_CLIENTS * 100%': '1750.0',
    'LOANS_TO_CLIENTS_NET / CLIENT_DEPOSITS * 100%': '41.05263157894737',
    'LONG_TERM_DEBT + LONG_TERM_LEASE_LIABILITIES + SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES': '136.0',
    'LONG_TERM_DEBT + SHORT_TERM_BORROWINGS': '61.0',
    'NET_DEBT / ANNUAL(EBITDA)': '0.8421052631578947',
    'NET_INCOME - ORGANIC_NET_INCOME - DEFERRED_TAX_ASSETS': '52.0',
    'NET_INCOME / AVG (TOTAL_ASSETS) * 100%': '122.80701754385966',
    'NET_INCOME / AVG (TOTAL_EQUITY)': '1.0411899313501143',
    'NET_INCOME / AVG (TOTAL_EQUITY) * 100%': '104.11899313501142',
    'NET_INCOME / AVG(TOTAL_EQUITY) * 100%': '104.11899313501142',
    'NET_INCOME / SALES_REVENUE * 100%': '227.5',
    'NET_INTEREST_INCOME + OTHER_INCOME_TOTAL - OPERATING_EXPENSES_BANKS': '67.0',
    'NET_INTEREST_INCOME / AVG (INTEREST_EARNING_ASSETS)': '0.6736842105263158',
    'NET_INTEREST_INCOME / AVG (INTEREST_EARNING_ASSETS) * 100%': '67.36842105263158',
    'NET_NPA / TOTAL_LOANS_TO_CLIENTS * 100%': '3250.0',
    'NET_OPERATING_CASH_FLOW + NET_INTEREST_PAID * (1 - INCOME_TAX / PROFIT_BEFORE_TAX_CONTINUING_OPERATIONS) - CAPEX': '-87.71153846153847',
    'NET_OPERATING_CASH_FLOW - CAPEX': '-28.0',
    'NET_OPERATING_CASH_FLOW / EBITDA * 100%': '134.21052631578948',
    'NET_OPERATING_CASH_FLOW – CAPEX': '-28.0',
    'NET_OTHER_ASSETS - PPE - INTANGIBLE_ASSETS - TOTAL_ASSOCIATE_INVESTMENTS': '-131.0',
    'OPERATING_EXPENSES_BANKS / AVG(TOTAL_ASSETS)': '0.20242914979757087',
    'OPERATING_EXPENSES_BANKS / AVG(TOTAL_ASSETS) * 100%': '20.24291497975709',
    'OPERATING_EXPENSES_BANKS / COST_TO_INCOME_RATIO': '0.4166666666666667',
    'OPERATING_EXPENSES_BANKS / (COST_TO_INCOME_RATIO / 100)': '41.66666666666667',
    # ⚠️ ПЕРЕСНЯТО ОСОЗНАННО (#146). Здесь стояла строка
    #     'OPERATING_INCOME_BANKS / OPERATING_EXPENSES_BANKS': '2.2'
    # — перевёрнутый Cost/Income: доход делили на расход. На живом банке это
    # давало «CIR = 4,1» вместо 24,4%, то есть 1/x. Формулу в реестре
    # развернули, ключ эталона сменился вместе с ней; значение 0,4545 = 1/2,2
    # на тех же синтетических входах, что и раньше.
    'OPERATING_EXPENSES_BANKS / OPERATING_INCOME_BANKS': '0.45454545454545453',
    'OPERATING_EXPENSES_BANKS / OPERATING_INCOME_BANKS * 100%': '45.45454545454545',
    'OPERATING_PROFIT + DEPRECIATION_AMORTIZATION': '150.0',
    'OPERATING_PROFIT / SALES_REVENUE * 100%': '202.5',
    'OTHER_ASSETS - TOTAL_CREDIT_ALLOWANCE': '41.0',
    'OTHER_LIABILITIES - JUNIOR_DEBT': '-46.0',
    'PROFIT_FROM_SALES + DEPRECIATION_AMORTIZATION': '151.0',
    'PROFIT_FROM_SALES / SALES_REVENUE * 100': '204.99999999999997',
    'PROVISION_FOR_NPA / GROSS_NPA * 100%': '255.26315789473685',
    'RISK_WEIGHTED_ASSETS / TOTAL_ASSETS': '0.15384615384615385',
    'RISK_WEIGHTED_ASSETS / TOTAL_ASSETS * 100%': '15.384615384615385',
    'SALES_REVENUE - COSTS_EXPENSES': '-4.0',
    'SALES_REVENUE / AVG (RIGHT_OF_USE_ASSETS + PPE)': '0.41279669762641896',
    'SALES_REVENUE / AVG (TOTAL_ASSETS)': '0.5398110661268556',
    'SALES_REVENUE – COSTS_EXPENSES': '-4.0',
    'SELLING_EXPENSES + ADMINISTRATIVE_EXPENSES': '106.0',
    'SHORT_TERM_ROU + LONG_TERM_ROU': '165.0',
    'SUBSIDIARY_ACQUISITIONS + ASSOCIATE_INVESTMENTS - ASSOCIATE_INCOMES': '88.0',
    'SUBSIDIARY_ACQUISITIONS + ASSOCIATE_INVESTMENTS – ASSOCIATE_INCOMES': '88.0',
    'TIER1_CAPITAL + TIER2_CAPITAL': '181.0',
    'TOTAL_ASSETS - TOTAL_EQUITY': '-14.0',
    'TOTAL_ASSETS / TOTAL_EQUITY': '0.8478260869565217',
    'TOTAL_CASH_EQUIVALENTS / (SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES) * 100%': '92.95774647887323',
    'TOTAL_CURRENT_ASSETS - TOTAL_CURRENT_LIABILITIES': '-53.0',
    'TOTAL_CURRENT_ASSETS / TOTAL_CURRENT_LIABILITIES': '0.43617021276595747',
    'TOTAL_DEBT - CASH_EQUIVALENTS': '24.0',
    'TOTAL_DEBT - CASH_EQUIVALENTS - BANK_DEPOSITS': '-5.0',
    'TOTAL_DEBT / ANNUAL(EBITDA)': '2.4210526315789473',
    'TOTAL_DEBT / ANNUAL(NET_OPERATING_CASH_FLOW)': '1.803921568627451',
    'TOTAL_DEBT / TOTAL_EQUITY': '1.0',
    'TOTAL_DEBT – CASH_EQUIVALENTS': '24.0',
    'TOTAL_DEBT – CASH_EQUIVALENTS - BANK_DEPOSITS': '-5.0',
    'TOTAL_LOANS_TO_CLIENTS + SECURITIES_ASSETS + INTERBANK_DEPOSITS': '99.0',
    'TOTAL_LOANS_TO_CLIENTS / CLIENT_DEPOSITS': '0.021052631578947368',
    'TOTAL_LOANS_TO_CLIENTS / CLIENT_DEPOSITS * 100%': '2.1052631578947367',
    'TOTAL_REGULATORY_CAPITAL / RISK_WEIGHTED_ASSETS * 100%': '233.33333333333334',
}


# ── регресс: старый eval-путь vs новый разбор в AST ──────────────────────────


def test_golden_covers_every_registry_formula():
    """Реестр не должен уезжать от эталона молча."""
    assert set(_all_registry_formulas()) == set(GOLDEN)


@pytest.mark.parametrize("formula", _all_registry_formulas())
def test_registry_formula_result_unchanged(formula: str):
    tokens = sorted(bx._formula_metric_tokens(formula))
    extracted = {token: _synthetic(token) for token in tokens}
    # Прошлый период отличается от текущего — иначе AVG(X) совпал бы с X
    # и формулы с усреднением проходили бы регресс «вхолостую».
    previous = {token: _synthetic(token) * 0.9 for token in tokens}
    result = bx._eval_calculated_formula(formula, extracted, {}, previous)
    assert repr(float(result)) == GOLDEN[formula]


# ── поведение, которое обязано было сохраниться ──────────────────────────────


def test_missing_input_raises_missing_formula_input():
    with pytest.raises(bx._MissingFormulaInput):
        bx._eval_formula_expr("SALES_REVENUE + EBITDA", {"SALES_REVENUE": 10.0})


def test_division_by_zero_propagates():
    with pytest.raises(ZeroDivisionError):
        bx._eval_formula_expr("SALES_REVENUE / EBITDA", {"SALES_REVENUE": 10.0, "EBITDA": 0.0})


def test_decimal_comma_still_reads_as_decimal_separator():
    assert bx._eval_formula_expr("SALES_REVENUE * 0,75", {"SALES_REVENUE": 100.0}) == 75.0


def test_percent_sign_is_editorial_not_modulo():
    env = {"EBITDA": 25.0, "SALES_REVENUE": 100.0}
    assert bx._eval_formula_expr("EBITDA / SALES_REVENUE * 100%", env) == 25.0


def test_optional_zero_deps_substitute_missing_inputs():
    value = bx._eval_calculated_formula(
        "ASSET_PURCHASES + ASSET_SALES_PROCEEDS",
        {"ASSET_PURCHASES": 10.0},
        {},
        None,
        optional_zero_deps={"ASSET_SALES_PROCEEDS"},
    )
    assert value == 10.0


def test_formula_default_abs_applies_to_inputs():
    value = bx._eval_calculated_formula(
        "INTEREST_PAID + 1",
        {"INTEREST_PAID": -5.0},
        {},
        None,
        formula_default_abs_enums={"INTEREST_PAID"},
    )
    assert value == 6.0


def test_abs_call_works():
    assert bx._eval_formula_expr("ABS(NET_INCOME)", {"NET_INCOME": -7.0}) == 7.0


def test_min_max_calls_work():
    env = {"A": 3.0, "B": 8.0}
    assert bx._eval_formula_expr("MIN(A, B)", env) == 3.0
    assert bx._eval_formula_expr("MAX(A, B)", env) == 8.0


def test_avg_falls_back_to_previous_period_env():
    value = bx._eval_calculated_formula(
        "AVG(TOTAL_ASSETS)", {"TOTAL_ASSETS": 100.0}, {}, {"TOTAL_ASSETS": 50.0}
    )
    assert value == 75.0


def test_avg_resolver_averages_two_latest_point_periods():
    resolver = bx._build_avg_resolver_for_period(
        "FY 2024",
        {"31.12.2023": {"TOTAL_ASSETS": 100.0}, "31.12.2024": {"TOTAL_ASSETS": 200.0}},
        {},
        ["31.12.2023", "31.12.2024"],
    )
    value = bx._eval_calculated_formula(
        "NET_INCOME / AVG(TOTAL_ASSETS)",
        {"NET_INCOME": 30.0},
        {},
        None,
        avg_resolver=resolver,
    )
    assert value == 30.0 / 150.0


def test_broken_formula_yields_none_not_crash():
    """`_calculate_metrics` глотает любую ошибку формулы — это контракт вызова."""
    result = bx._calculate_metrics(
        [("BROKEN", "Сломанная", "SALES_REVENUE +", ["SALES_REVENUE"])],
        {"SALES_REVENUE": 10.0},
    )
    assert result["BROKEN"] is None


# ── безопасность: что старый eval пропускал ──────────────────────────────────


def test_subclasses_escape_is_rejected():
    escape = (
        "[c for c in ().__class__.__base__.__subclasses__() "
        "if c.__name__=='BuiltinImporter'][0].load_module('os')"
    )
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr(escape, {})


def test_attribute_access_is_rejected():
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr("TOTAL_ASSETS.__class__", {"TOTAL_ASSETS": 1.0})


def test_unknown_function_is_rejected():
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr("EXP(TOTAL_ASSETS)", {"TOTAL_ASSETS": 1.0})
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr("__import__('os')", {})


def test_disallowed_operators_are_rejected():
    env = {"A": 2.0, "B": 3.0}
    for expr in ("A ** B", "A > B", "A // B", "A if B else A"):
        with pytest.raises(bx._FormulaError):
            bx._eval_formula_expr(expr, env)


def test_string_constant_is_rejected():
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr("'os'", {})


def test_syntax_error_is_explicit():
    with pytest.raises(bx._FormulaError):
        bx._eval_formula_expr("SALES_REVENUE +", {"SALES_REVENUE": 1.0})


def test_ambiguous_comma_inside_multiarg_call_is_refused():
    """`MIN(A, 1,000)` раньше молча становился `MIN(A, 1.0)`.

    Неоднозначна запятая ТОЛЬКО внутри вызова с несколькими аргументами: там
    она одновременно и десятичный разделитель, и разделитель аргументов.
    """
    with pytest.raises(bx._FormulaError, match="неоднозначн"):
        bx._eval_formula_expr("MIN(NET_DEBT, 1,000)", {"NET_DEBT": 5000.0})


@pytest.mark.parametrize("expr", ["A * 0,125", "A * 0,075", "A * 0,75", "A / 0,5"])
def test_plain_decimal_comma_stays_legal(expr):
    """Дробь с запятой ВНЕ MIN/MAX законна — так написан реестр.

    Первая версия защиты резала «0,125» и «0,075» как «похоже на разряды»:
    легальная дробь с тремя знаками ложно отвергалась (нашла проверка).
    """
    got = bx._eval_formula_expr(expr, {"A": 8.0})
    assert isinstance(got, float)
