"""Знак отчёта у расчётной строки виден ЧИТАТЕЛЮ и не виден ФОРМУЛАМ.

ЦЕНА (состязательная проверка 21.08.2026). Правка `c6c7854` научила расчётную
строку печататься в знаке отчёта — CAPEX Северстали приехал −43 591 вместо
+43 591, и это верно. Тем же движением она сломала КАЖДОГО его потребителя:
`result[CAPEX]` уезжал минусом во входы следующих формул, а формулы реестра
писаны арифметикой МОДУЛЕЙ (`FREE_CASH_FLOW = NET_OPERATING_CASH_FLOW - CAPEX`
вычитает, потому что признак `formula_default_abs_behavior` знак снимает). На
тех же входах получалось `60 000 − (−43 591) = 103 591` — свободный поток
БОЛЬШЕ операционного при капзатратах 43,6 млрд, и в разбор это поехало бы как
«компания генерирует кэш». Подпись строки при этом внутренне сходилась.

ПОЧЕМУ ЭТОГО НЕ ПОЙМАЛ НИКТО. `test_formula_eval.py::GOLDEN` считает на
синтетике 1..97 («ноль исключён намеренно»), отрицательных входов там нет
вовсе — снимок структурно слеп к знаку. Границы effective registry держат девять
банковских enum'ов, ни FCF, ни покрытия среди них нет. Скорер расчётные строки
выбрасывает с обеих сторон. Зонд-лечение проверяющего оставил зелёными все 146
тестов трёх ближайших файлов.

ЧТО ОХРАНЯЕТСЯ ЗДЕСЬ — сам инвариант, а не его реализация: знак, который
правило `_result_is_outflow` ставит строке, есть ОТРИЦАНИЕ, и он снимается тем
же отрицанием при обратной подстановке. Значит между прогоном на входах в знаках
отчёта и прогоном на тех же входах в модулях расчётные строки обязаны разойтись
ТОЛЬКО ЗНАКОМ и ни на одной строке — величиной.

ЗАМЕР, КОТОРЫМ ЭТО ПРОВЕРЕНО НА ДЕЛЕ (21.08.2026, разметка
`benchmarks/vendor/.../golden/*/gt/*.csv`, 112 документов, 5 341 расчётная
клетка): правило срабатывает на 261 клетке, и ровно эти 261 отличаются от базы
`4024859`. Множества совпадают побайтово, симметрическая разность пуста — ни
один потребитель не сдвинулся.

ПРОВЕРКА СТОРОЖА ОТКАТОМ (каждая мутация прогнана, см. отчёт волны):
  1. снять снятие знака у расчётного входа в `_formula_env` (ветка
     `if str(key) in outflow`) — краснеет `test_the_display_sign_moves_no_row_by_magnitude`
     на восьми ячейках из десяти и обе проверки цепочки;
  2. `value = -value` → `value = -abs(value)` в `_eval_calculated_formula` —
     краснеет `test_a_net_inflow_is_printed_as_an_inflow`;
  3. `return bool(added) and added <= abs_applied` → `return added <= abs_applied`
     — краснеет `test_a_row_without_a_single_found_addend_is_not_an_outflow`;
  4. не класть `enum` в `outflow_enums` — краснеют обе проверки цепочки.
"""
from __future__ import annotations

import pytest

from finengine.skills.bulk_extractor import (
    _calculate_metrics,
    _calculate_metrics_for_periods,
)
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

# Числа Северстали Q1-2025 (ячейка ifrs_nf_ru), знаки — как в отчёте: покупки в
# скобках. Операционный поток и выручка взяты из того же прогона владельца.
SEVERSTAL = {
    "ASSET_PURCHASES": -43591.0,
    "NET_OPERATING_CASH_FLOW": 60000.0,
    "SALES_REVENUE": 178733.0,
    "EBITDA": 50000.0,
    "INTEREST_PAID": -2907.0,
    "INTEREST_RECEIVED": 0.0,
    "NET_INCOME": 25700.0,
    "DIVIDENDS_SHAREHOLDERS": -12800.0,
}


def _severstal_run() -> dict:
    registry = registry_for_profile("ifrs_nf_ru")
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    return _calculate_metrics(
        registry["calculated"], dict(SEVERSTAL), formula_default_abs_enums=abs_enums
    )


def test_the_outflow_itself_prints_the_report_sign():
    """Половина, ради которой правило заведено: строка уходит в знак отчёта."""
    result = _severstal_run()
    assert result["CAPEX"] == pytest.approx(-43591.0)
    assert result["NET_INTEREST_PAID"] == pytest.approx(-2907.0)


def test_free_cash_flow_still_subtracts_the_capex_it_prints_negative():
    """Тот же прогон, та же клетка: потребитель обязан увидеть МОДУЛЬ.

    60 000 − 43 591 = 16 409. Величина базы `4024859`. Если сюда приедет
    103 591, свободный поток стал больше операционного — арифметически
    невозможный результат, о котором читателю ничто не скажет.
    """
    result = _severstal_run()
    assert result["FREE_CASH_FLOW"] == pytest.approx(16409.0), (
        f"CAPEX напечатан как {result['CAPEX']}, и это верно, но в формулу "
        f"свободного потока он обязан войти модулем. Пришло "
        f"{result['FREE_CASH_FLOW']} вместо 16 409."
    )


@pytest.mark.parametrize(
    "enum, expected",
    [
        ("CAPEX_TO_REVENUE", 24.388892929677226),
        ("INTEREST_COVERAGE_RATIO", 17.19986240110079),
        ("FCF_TO_INTEREST_PAID", 5.644650842793258),
        # ⚠️ Ровно в сто раз больше базы `4024859`: доходности переведены в
        # проценты (R1), и это ДРУГОЕ правило, не конвенция знака. Величина,
        # которую стережёт этот сторож, — та же.
        ("DIVIDEND_PAYOUT_RATIO", 49.80544747081712),
    ],
)
def test_a_consumer_of_a_signed_row_keeps_the_value_it_had_before_the_rule(
    enum: str, expected: float
):
    """Доли и покрытия базы `4024859`: знак строки до них доходить не должен."""
    result = _severstal_run()
    assert result[enum] == pytest.approx(expected), (
        f"{enum}: потребитель увидел знак экрана вместо модуля."
    )


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_the_display_sign_moves_no_row_by_magnitude(cell: str):
    """Главный инвариант, на всех десяти ячейках и на всём их реестре.

    Два прогона одного реестра: входы в знаках отчёта и те же входы модулями.
    Во втором признак `formula_default_abs_behavior` не срабатывает ни разу,
    значит и правило знака не срабатывает — это прогон БЕЗ конвенции экрана.
    Расхождение между ними обязано быть только знаком: любая разошедшаяся
    ВЕЛИЧИНА означает, что знак экрана протёк во входы чужой формулы.
    """
    registry = registry_for_profile(cell)
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    report, module = {}, {}
    for index, row in enumerate(registry["extractable"]):
        enum = str(row.get("enum", "")).strip().upper()
        if not enum:
            continue
        # Разные числа у разных показателей: на одинаковых совпадение сошлось бы
        # случайно и сторож молчал бы о настоящем расхождении.
        value = 100.0 + index * 7.0
        module[enum] = value
        report[enum] = -value if row.get("formula_default_abs_behavior") else value

    signed = _calculate_metrics(
        registry["calculated"], report, formula_default_abs_enums=abs_enums
    )
    plain = _calculate_metrics(
        registry["calculated"], module, formula_default_abs_enums=abs_enums
    )

    assert {e for e, v in signed.items() if v is not None} == {
        e for e, v in plain.items() if v is not None
    }, f"{cell}: конвенция экрана поменяла СОСТАВ посчитанных строк"

    flipped = []
    for enum, value in signed.items():
        other = plain.get(enum)
        if value is None or other is None:
            continue
        assert abs(value) == pytest.approx(abs(other), rel=1e-9, abs=1e-9), (
            f"{cell}/{enum}: величина строки зависит от того, в каком знаке "
            f"пришли входы ({value} против {other}). Знак экрана протёк в "
            f"формулу-потребителя."
        )
        if value != other:
            flipped.append(enum)

    # ДОСТИЖИМОСТЬ: без этой проверки сторож зеленел бы и на реестре, где
    # правило не срабатывает вовсе, — то есть проверял бы пустоту.
    assert flipped, f"{cell}: правило знака не сработало ни на одной строке"


def test_a_net_inflow_is_printed_as_an_inflow():
    """Модуль сам отрицателен — поток ВХОДЯЩИЙ, и минус ему не ставится.

    Числа DNB 6M 2025 (ячейка ifrs_bank_en): банк уплатил процентов 25 000 и
    получил 71 491, `NET_INTEREST_PAID = INTEREST_PAID − INTEREST_RECEIVED`
    даёт −46 491 в арифметике модулей. Напечатать это минусом значило бы
    сказать «уплачено 46 491» о полученных деньгах. На разметке таких
    срабатываний 23 из 261.
    """
    registry = registry_for_profile("ifrs_bank_en")
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    result = _calculate_metrics(
        registry["calculated"],
        {"INTEREST_PAID": -25000.0, "INTEREST_RECEIVED": 71491.0},
        formula_default_abs_enums=abs_enums,
    )
    assert result["NET_INTEREST_PAID"] == pytest.approx(46491.0)


def test_a_row_without_a_single_found_addend_is_not_an_outflow():
    """Слагаемых со снятым знаком не нашлось ни одного — правило молчит.

    Выручка ноль, расходы −100: валовая прибыль это УБЫТОК −100. Единственное
    слагаемое формулы (выручка) нулевое, то есть множество найденных слагаемых
    пусто, — а пустое множество вложено в любое. Без проверки непустоты правило
    сочло бы строку оттоком и напечатало +100, превратив убыток в прибыль.
    """
    registry = registry_for_profile("ifrs_nf_ru")
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    result = _calculate_metrics(
        registry["calculated"],
        {"SALES_REVENUE": 0.0, "COSTS_EXPENSES": -100.0},
        formula_default_abs_enums=abs_enums,
    )
    assert result["GROSS_PROFIT"] == pytest.approx(-100.0)


def test_the_average_of_a_signed_row_averages_modules():
    """Второй сборщик окружения — путь `AVG(...)` — держит то же правило.

    `_env_from_period_values` собирает числа для `AVG(...)` мимо `_formula_env`,
    и правило, заведённое в одном сборщике, на этом пути не сработало бы.
    Реестровой формулы `AVG(...)` поверх расчётного оттока сегодня нет — состав
    здесь свой, но считает его боевой `_calculate_metrics_for_periods`.
    """
    calc_defs = [
        ("CAPEX", "Капзатраты", "ASSET_PURCHASES", ["ASSET_PURCHASES"]),
        ("MEAN_CAPEX", "Средние капзатраты", "AVG(CAPEX)", ["CAPEX"]),
    ]
    by_period = {
        "31.12.2024": {"ASSET_PURCHASES": -100.0},
        "31.12.2025": {"ASSET_PURCHASES": -300.0},
        "FY 2025": {},
    }
    result = _calculate_metrics_for_periods(
        calc_defs,
        by_period,
        ["31.12.2024", "31.12.2025", "FY 2025"],
        formula_default_abs_enums={"ASSET_PURCHASES"},
    )
    assert result["31.12.2025"]["CAPEX"] == pytest.approx(-300.0)
    assert result["FY 2025"]["MEAN_CAPEX"] == pytest.approx(200.0), (
        "среднее посчитано по напечатанным минусам, а не по модулям"
    )


def test_the_previous_period_of_a_signed_row_substitutes_a_module():
    """Тот же инвариант для сравнительного периода парного прогона."""
    from finengine.skills.bulk_extractor import _calculate_metrics_pair

    calc_defs = [
        ("CAPEX", "Капзатраты", "ASSET_PURCHASES", ["ASSET_PURCHASES"]),
        ("CAPEX_GROWTH", "Прирост капзатрат", "CAPEX - AVG(CAPEX)", ["CAPEX"]),
    ]
    current, previous = _calculate_metrics_pair(
        calc_defs,
        {"ASSET_PURCHASES": -300.0},
        {"ASSET_PURCHASES": -100.0},
        formula_default_abs_enums={"ASSET_PURCHASES"},
    )
    assert current["CAPEX"] == pytest.approx(-300.0)
    assert previous["CAPEX"] == pytest.approx(-100.0)
    # AVG берёт модули обоих периодов: (300 + 100) / 2 = 200, и вычитается он
    # из модуля текущего — 300 − 200 = 100. Знак результата ставится потом.
    assert current["CAPEX_GROWTH"] == pytest.approx(100.0)


def _traced(cell: str, extracted: dict) -> dict:
    registry = registry_for_profile(cell)
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    trace: dict[str, dict] = {}
    result = _calculate_metrics(
        registry["calculated"],
        extracted,
        formula_default_abs_enums=abs_enums,
        trace=trace,
        period="Q1 2025",
    )
    return {"result": result, "trace": trace}


def test_the_consumer_signature_says_what_it_did_to_the_signed_input():
    """Подпись потребителя обязана воспроизводить ЕГО число по ЕГО входам (Б5).

    В строке CAPEX читатель видит −43 591, а в формулу свободного потока он
    входит модулем. Не назвать преобразование — значит показать читателю спор
    двух строк там, где спора нет: `60 000 − (−43 591)` подписи не сходится с
    16 409 в клетке.
    """
    run = _traced("ifrs_nf_ru", dict(SEVERSTAL))
    signature = run["trace"]["FREE_CASH_FLOW"]
    by_enum = {i["enum"]: i for i in signature["inputs"]}
    assert by_enum["CAPEX"]["transform"] == "sign"
    assert by_enum["CAPEX"]["value"] == pytest.approx(43591.0)
    restated = by_enum["NET_OPERATING_CASH_FLOW"]["value"] - by_enum["CAPEX"]["value"]
    assert restated == pytest.approx(run["result"]["FREE_CASH_FLOW"])


def test_the_label_of_a_signed_input_is_negation_and_not_a_modulus():
    """Ярлык `sign`, а не `abs`, — и это различие видно только на притоке.

    Компания получила процентов больше, чем уплатила: строка
    `NET_INTEREST_PAID` печатается ПЛЮСОМ (46 491 получено), а в формулу
    покрытия входит минусом — это по-прежнему одно и то же отрицание. Назови
    его `abs(...)`, и подпись покажет +46 491 там, где в расчёт пошло −46 491,
    а покрытие в клетке отрицательное.
    """
    run = _traced(
        "ifrs_nf_ru", {"INTEREST_PAID": -25000.0, "INTEREST_RECEIVED": 71491.0,
                       "EBITDA": 100000.0}
    )
    assert run["result"]["NET_INTEREST_PAID"] == pytest.approx(46491.0)
    signature = run["trace"]["INTEREST_COVERAGE_RATIO"]
    by_enum = {i["enum"]: i for i in signature["inputs"]}
    assert by_enum["NET_INTEREST_PAID"]["transform"] == "sign"
    assert by_enum["NET_INTEREST_PAID"]["value"] == pytest.approx(-46491.0)
    restated = by_enum["EBITDA"]["value"] / by_enum["NET_INTEREST_PAID"]["value"]
    assert restated == pytest.approx(run["result"]["INTEREST_COVERAGE_RATIO"])


def test_a_printed_row_overrides_the_calculated_label_of_the_same_metric():
    """Показатель, заведённый И извлекаемым, И расчётным, приходит во входы
    ДВАЖДЫ, и печатная строка перекрывает посчитанную.

    Пометку расчётной половины тогда надо снять: она описывала бы уже не то
    число. Отчёт Amazon печатает капзатраты величиной без скобок — вход равен
    напечатанному, преобразования не было никакого, и подпись `-Капзатраты`
    показала бы читателю −43 591 там, где в расчёт пошло +43 591.
    """
    run = _traced(
        "ifrs_nf_ru",
        {"CAPEX": 43591.0, "ASSET_PURCHASES": -43591.0,
         "NET_OPERATING_CASH_FLOW": 60000.0},
    )
    by_enum = {i["enum"]: i for i in run["trace"]["FREE_CASH_FLOW"]["inputs"]}
    assert by_enum["CAPEX"]["value"] == pytest.approx(43591.0)
    assert by_enum["CAPEX"]["transform"] == ""


def test_the_public_door_of_the_calc_skill_shows_the_same_chain():
    """Прогон через ПРОИЗВОДИТЕЛЯ строк, а не через вычислитель напрямую.

    Расчётные строки делают пять мест (`two_stage_extractor`,
    `streams_full_context`, `ratios`, `skills/aifa/extractor`,
    `skills/aifa_baseline/extractor`), и все пять зовут одни и те же
    `_calculate_metrics*`. `compute_ratios` — единственное из пяти, что
    вызывается без документа, поэтому цепочку целиком проверяем им: если
    правило когда-нибудь заведут в производителе, а не в движке, эта проверка
    и проверка выше разойдутся.
    """
    from finengine.skills.ratios import compute_ratios

    period = "Q1 2025"
    metrics = [
        {"enum": enum, "found": True, "value_current": value, "value_prev": None,
         "period_values": [{"period": period, "value": value, "unit": "млн. руб."}]}
        for enum, value in SEVERSTAL.items()
    ]
    rows = {r["enum"]: r for r in compute_ratios(metrics, "ifrs_nf_ru",
                                                 period_current=period)}
    assert rows["CAPEX"]["value_current"] == pytest.approx(-43591.0)
    assert rows["FREE_CASH_FLOW"]["value_current"] == pytest.approx(16409.0)
    assert rows["CAPEX_TO_REVENUE"]["value_current"] == pytest.approx(24.388892929677226)
    assert rows["INTEREST_COVERAGE_RATIO"]["value_current"] == pytest.approx(17.19986240110079)


def test_a_cancelled_outflow_is_zero_and_not_minus_zero():
    """Покупки ровно перекрыты продажами — ноль. Но `-0.0` печатают ДВА места
    по-разному: книга роли даёт «0», `toLocaleString('ru-RU')` на экране «-0».
    Одна клетка, два ответа на один вопрос — гасим у источника.
    """
    registry = registry_for_profile("ifrs_nf_ru")
    abs_enums = {
        str(m.get("enum", "")).strip().upper()
        for m in registry["extractable"]
        if m.get("formula_default_abs_behavior")
    }
    result = _calculate_metrics(
        registry["calculated"],
        {"ASSET_PURCHASES": -100.0, "ASSET_SALES_PROCEEDS": 100.0},
        formula_default_abs_enums=abs_enums,
    )
    import math

    assert result["CAPEX"] == 0.0
    assert not math.copysign(1.0, result["CAPEX"]) < 0, "в клетку уехал -0.0"
