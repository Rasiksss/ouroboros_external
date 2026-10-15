"""Текст методики и формула реестра — про ОДНУ величину (#146).

ПОЧЕМУ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. Про Cost/Income в продукте было написано дважды:
в тексте методики (`skills/analysis/methodology.py`) — «операционные расходы ÷
доход до резервов», а в реестре (`data/registry/*.json`) — наоборот, «доход ÷
расходы». Оба текста уезжали в ОДИН промпт как равно-авторитетные, и никто их
между собой не сверял: тесты проверяли, что промпт не изменился, и что формула
считается так же, как вчера. Обе проверки были зелёными ровно потому, что
ошибка не менялась. У здорового банка выходил «CIR 4,1» — то есть 410%.

Здесь проверяется то, чего не проверял никто: что текст и формула — про одно и
то же, и что посчитанное имеет смысл. Проверка адресная: связь держит поле
`Element.registry_enum`, а не отдельная таблица соответствий (иначе таблица
стала бы ТРЕТЬИМ источником истины и разъехалась бы со своими двумя).

Расхождения, где однозначного ответа НЕТ (уровень консолидации ROE, база CoR,
состав процентных активов), сюда не «чинятся»: они записаны как ДАННЫЕ в
`ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ` — чтобы висеть на виду до решения владельца, а не
раствориться в устном обсуждении.
"""
from __future__ import annotations

import pytest

from finengine.skills import bulk_extractor as bx
from finengine.skills.analysis import methodology as md
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

BANK_PROFILES = [p for p in PROFILE_IDS if "_bank_" in p]


def sanity_warning(enum: object, value: object, unit: str = "") -> str | None:
    """Compatibility assertion helper backed by the effective registry."""
    meta = registry_for_profile("ifrs_bank_ru")["calculated_meta"]
    return bx._sanity_note(enum, value, unit, meta) or None

# Ячейки, в которых показателя из текста методики НЕТ ВООБЩЕ. Промпт для банка
# при этом обещает все четыре коэффициента — то есть по индийскому банку модель
# обязана нарушить одно из двух правил. Это дорожная карта (доукомплектовать
# ячейку), а не однострочная правка; пока — на виду.
ИЗВЕСТНЫЕ_ПРОБЕЛЫ_ЯЧЕЕК = {
    "ind_gaap_bank_en": {"NET_INTEREST_MARGIN", "COST_OF_RISK_GENERAL", "COST_TO_INCOME_RATIO"},
}

# Текст методики требует одного, реестр считает другое, и обе версии
# профессионально защитимы → решает владелец продукта, не тест.
ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ = {
    "bank.formula.roe": (
        "текст требует прибыль и капитал АКЦИОНЕРОВ (без миноритариев и AT1), "
        "реестр считает по уровню ГРУППЫ (NET_INCOME / AVG(TOTAL_EQUITY) с NCI); "
        "входа NET_INCOME_SHAREHOLDERS в ячейке нет"
    ),
    "bank.formula.cor": (
        "текст запрещает агрегированную P&L-строку ECL и требует ВАЛОВЫЕ кредиты "
        "в знаменателе, реестр берёт CHANGE_IN_EXPECTED_CREDIT_LOSSES (по всем "
        "финансовым активам) и TOTAL_LOANS_TO_CLIENTS (чистые)"
    ),
    "bank.formula.nim": (
        "текст перечисляет средства в ЦБ и репо среди процентно-приносящих активов "
        "и просит только ДОЛГОВЫЕ ЦБ; INTEREST_EARNING_ASSETS в реестре — кредиты + "
        "все ЦБ + МБК"
    ),
}


# Ячейка `rsbu_bank_ru` заведена 15.08 (#250) НЕДОУКОМПЛЕКТОВАННОЙ: 11
# расчётных строк против 32 у `ifrs_bank_ru`, и трёх ключевых банковских
# коэффициентов в ней нет вовсе (посчитано по самим файлам реестра).
# В `ИЗВЕСТНЫЕ_ПРОБЕЛЫ_ЯЧЕЕК` её НЕ вносим намеренно: тот список гасит
# проверку насовсем, а `xfail(strict=True)` покраснеет в день, когда ячейку
# доукомплектуют, и заставит снять пометку. Пробел обязан уйти, а не осесть.
ЖДЁТ_ДОУКОМПЛЕКТОВАНИЯ_RSBU_BANK = {
    "bank.formula.nim", "bank.formula.cor", "bank.formula.cir",
}


def _formula_elements() -> list:
    """Элементы методики, которые ОПИСЫВАЮТ показатель реестра."""
    return [e for e in md.base().elements.values() if e.kind == "formula" and e.registry_enum]


def _formula_params() -> list:
    """Те же элементы, но три из них помечены ожидаемо красными."""
    out = []
    for element in _formula_elements():
        marks = []
        if element.id in ЖДЁТ_ДОУКОМПЛЕКТОВАНИЯ_RSBU_BANK:
            marks = [pytest.mark.xfail(
                strict=True,
                reason=f"ждёт #250: в ячейке rsbu_bank_ru нет {element.registry_enum} "
                       f"— банковский разбор по РСБУ выходит без этого коэффициента, "
                       f"и пользователю об этом не сказано",
            )]
        out.append(pytest.param(element, id=element.id, marks=marks))
    return out


def _calc_by_enum(profile_id: str) -> dict[str, str]:
    return {
        str(enum).strip().upper(): formula
        for enum, _name, formula, _deps in registry_for_profile(profile_id)["calculated"]
    }


# ── 1. текст ссылается на существующий показатель ────────────────────────────


@pytest.mark.parametrize("element", _formula_params())
def test_described_metric_exists_in_bank_cells(element):
    """Описали формулу словами — она обязана быть в реестре и СЧИТАТЬСЯ.

    Падение значит: в промпт уехало определение показателя, которого движок не
    считает, и модель получила задачу, выполнимую только счётом в уме.
    """
    missing = {
        pid for pid in BANK_PROFILES
        if element.registry_enum not in _calc_by_enum(pid)
        and element.registry_enum not in ИЗВЕСТНЫЕ_ПРОБЕЛЫ_ЯЧЕЕК.get(pid, set())
    }
    assert not missing, (
        f"{element.id} описывает {element.registry_enum}, которого нет в ячейках: "
        f"{sorted(missing)} — заведи формулу в реестр или убери определение из текста"
    )


def test_known_cell_gaps_still_gaps():
    """Пробел закрыли — удали его из списка, иначе список станет враньём."""
    for pid, gaps in ИЗВЕСТНЫЕ_ПРОБЕЛЫ_ЯЧЕЕК.items():
        present = gaps & set(_calc_by_enum(pid))
        assert not present, (
            f"{pid}: {sorted(present)} уже есть в реестре — убери из "
            f"ИЗВЕСТНЫЕ_ПРОБЕЛЫ_ЯЧЕЕК"
        )


def test_known_divergences_reference_live_elements():
    """Список расхождений адресует существующие элементы, а не прошлое."""
    ids = {e.id for e in md.base().elements.values()}
    assert not (set(ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ) - ids)


# ── 2. направление формулы: Cost/Income ──────────────────────────────────────


@pytest.mark.parametrize("profile_id", BANK_PROFILES)
def test_cost_to_income_is_expenses_over_income(profile_id):
    """Расходы в ЧИСЛИТЕЛЕ. Это и есть дефект #146, и ответ здесь однозначный.

    Проверяем не текст формулы, а её ПОВЕДЕНИЕ: банк с доходом 3451 и расходом
    842 обязан получить около 24,4%, а не 410%.
    """
    formula = _calc_by_enum(profile_id).get("COST_TO_INCOME_RATIO")
    if formula is None:
        pytest.skip(f"{profile_id}: показателя в ячейке нет")
    value = bx._eval_formula_expr(
        formula, {"OPERATING_INCOME_BANKS": 3451.0, "OPERATING_EXPENSES_BANKS": 842.0}
    )
    assert value == pytest.approx(842 / 3451 * 100, rel=1e-9), (
        f"{profile_id}: Cost/Income считается наоборот — {formula}"
    )


@pytest.mark.parametrize("profile_id", BANK_PROFILES)
def test_operating_income_via_cir_recovers_operating_income(profile_id):
    """Потребитель CIR сходится сам с собой.

    OPERATING_INCOME_CIR = расходы ÷ CIR — эта формула написана против
    ПРАВИЛЬНОГО определения (расход÷доход) и на перевёрнутом CIR давала мусор
    (205 вместо 3451). Значит она же — независимая проверка направления.
    """
    calc = _calc_by_enum(profile_id)
    if "OPERATING_INCOME_CIR" not in calc:
        pytest.skip(f"{profile_id}: показателя в ячейке нет")
    cir = bx._eval_formula_expr(
        calc["COST_TO_INCOME_RATIO"],
        {"OPERATING_INCOME_BANKS": 3451.0, "OPERATING_EXPENSES_BANKS": 842.0},
    )
    восстановленный = bx._eval_formula_expr(
        calc["OPERATING_INCOME_CIR"],
        {"OPERATING_EXPENSES_BANKS": 842.0, "COST_TO_INCOME_RATIO": cir},
    )
    assert восстановленный == pytest.approx(3451.0, rel=1e-9)


# ── 3. единицы: безразмерное не подписывается деньгами ───────────────────────


def _all_calculated_rows():
    for profile_id in PROFILE_IDS:
        registry = registry_for_profile(profile_id)
        for enum, _name, formula, _deps in registry["calculated"]:
            yield f"{profile_id}.json", {
                "enum": enum,
                "formula": formula,
                **registry["calculated_meta"].get(enum, {}),
            }


ДЕНЕЖНАЯ_ПОДПИСЬ = "млн. руб."


@pytest.mark.parametrize(
    ("file_name", "row"),
    list(_all_calculated_rows()),
    ids=[f"{n}:{r['enum']}" for n, r in _all_calculated_rows()],
)
def test_dimensionless_ratio_never_gets_money_unit(file_name, row):
    """ROE не должен уезжать в промпт как «0,25 млн. руб.».

    Проверяем через тот же помощник, которым подписывает движок, — иначе тест
    охранял бы свою копию правила.

    Строки с явным `unit_kind` пропускаем: там единицу назвал человек, зная,
    что структура врёт (OPERATING_INCOME_CIR выглядит отношением, а это деньги).
    Ловим именно ВЫВЕДЕННУЮ подпись — ту, что появляется молча.
    """
    if str(row.get("unit_kind", "")).strip():
        return
    unit = bx.unit_for_calculated(row["formula"], ДЕНЕЖНАЯ_ПОДПИСЬ)
    if unit != ДЕНЕЖНАЯ_ПОДПИСЬ:
        return
    assert bx.formula_dimension(row["formula"]) != "ratio", (
        f"{file_name}/{row['enum']}: безразмерный коэффициент подписан деньгами — "
        f"{row['formula']}"
    )


# Показатели, которые ЧИТАЮТСЯ ПРОЦЕНТОМ: доходность, маржа, доля, стоимость
# риска, ставка. Решение владельца 20.08.2026 — всё это семейство считается в
# процентах, а не в долях. До него ROE выходил долей (0,04) рядом с маржой в
# процентах (21,18%), а ROA был процентом в нефинансовых ячейках и долей в
# банковских: два показателя с одним словом «рентабельность» отличались на
# глаз в сто раз.
#
# ⚠️ СПИСОК ВЕДЁТСЯ ПО СМЫСЛУ ФОРМУЛЫ, А НЕ ПО ИМЕНИ. Предикат по именам
# затягивает сюда INTEREST_COVERAGE_RATIO («ratio» в имени), а покрытие
# процентов читается РАЗАМИ — оно в `РАЗЫ` ниже и сотни не получает.
ПРОЦЕНТНОЕ_СЕМЕЙСТВО = {
    "ROE", "ROE_SHAREHOLDERS", "ROA",
    "NET_INTEREST_MARGIN", "ASSET_YIELD", "FUNDING_COST",
    "COST_OF_RISK_GENERAL", "COST_TO_INCOME_RATIO", "COST_TO_ASSETS",
    "STAGE3_RATIO", "STAGE3_COVERAGE", "RETAIL_DEPOSIT_SHARE",
    "RWA_DENSITY", "LOANS_TO_DEPOSITS", "LOANS_TO_DEPOSITS_NET",
    "DIVIDEND_PAYOUT_RATIO",
    # маржи и доли, бывшие процентами и до решения
    "EBITDA_MARGIN", "EBIT_MARGIN", "GROSS_MARGIN", "NET_INCOME_MARGIN",
    "PROFIT_FROM_SALES_MARGIN", "CAPEX_TO_REVENUE", "OCF_TO_EBITDA",
    "SHORT_TERM_PORTION_OF_DEBT",
    # ⚠️ CASH_TO_SHORT_TERM_DEBT стоит здесь как ФАКТ, а не как решение: по
    # смыслу это покрытие («денег хватает на 1,4 краткосрочного долга»), и
    # место ему в `РАЗЫ`. Но сотня в его формуле стояла ЗАДОЛГО до перевода
    # доходностей в проценты (проверено по 2f6ce41 — формула побайтово та же),
    # то есть снятие поменяло бы уже опубликованные числа. Это отдельное
    # решение владельца; пока оно не принято, сторож держит показатель в том
    # классе, в котором он реально живёт, — иначе не держит вовсе.
    "CASH_TO_SHORT_TERM_DEBT",
    "CORPORATE_LOANS_TO_DEPOSITS_RATIO", "CASA_RATIO", "GROSS_NPA_RATIO",
    "NET_NPA_RATIO", "PROVISION_COVERAGE_RATIO", "CRAR_RATIO",
}

# Читаются РАЗАМИ: покрытие процентов, долг к капиталу, активы к капиталу,
# оборачиваемость, долг к EBITDA. Сотня здесь — дефект, а не недоделка.
РАЗЫ = {
    "INTEREST_COVERAGE_RATIO", "EBITDA_TO_INTEREST_PAID", "FCF_TO_INTEREST_PAID",
    "FCF_TO_INTEREST_PAID_CIB", "ADJ_OCF_TO_INTEREST_PAID",
    "RATIO_NET_DEBT_EBITDA", "RATIO_TOTAL_DEBT_EBITDA", "RATIO_TOTAL_DEBT_NOCF",
    "DEBT_TO_EQUITY_RATIO", "ASSETS_TO_EQUITY_RATIO",
    "CURRENT_RATIO", "QUICK_RATIO", "ASSET_TURNOVER", "FIXED_ASSET_TURNOVER",
}

# Читаются ДНЯМИ: оборачиваемости и денежный цикл. Структура формулы здесь
# врёт (деньги ÷ деньги × 365 выглядит безразмерным), поэтому единицу называет
# словом `unit_kind: "days"` — и снять это слово можно молча, что и случилось
# в `rsbu_nf_ru` до 21.08: цикл в 61 день печатался как «61 млн. руб.».
ДНИ = {
    "ACCOUNTS_PAYABLE_TURNOVER", "ACCOUNTS_RECEIVABLE_TURNOVER",
    "INVENTORIES_TURNOVER", "CASH_CONVERSION_CYCLE",
}


def _расчётные(profile_id: str):
    """(enum, формула, единица) по ячейке — единицу считает сам движок."""
    reg = registry_for_profile(profile_id)
    meta = reg["calculated_meta"]
    for enum, _name, formula, _deps in reg["calculated"]:
        key = str(enum).strip().upper()
        unit = bx.unit_for_calculated(
            formula, ДЕНЕЖНАЯ_ПОДПИСЬ, meta.get(key, {}).get("unit_kind", "")
        )
        yield key, formula, unit


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_percent_family_is_measured_in_percent(profile_id):
    """У каждого показателя процентного семейства единица «%».

    Единица НЕ заводится отдельным полем: её считает `unit_for_calculated` по
    наличию сотни в формуле. Второй механизм подписи означал бы два ответа на
    один вопрос, и они разошлись бы — как разошлись сами формулы.
    """
    плохие = [
        f"{enum}: единица {unit!r} — {formula}"
        for enum, formula, unit in _расчётные(profile_id)
        if enum in ПРОЦЕНТНОЕ_СЕМЕЙСТВО and unit != "%"
    ]
    assert not плохие, f"{profile_id}: не в процентах — {плохие}"


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_ratios_read_in_times_never_get_a_hundred(profile_id):
    """Обратная сторона: покрытие и оборачиваемость процентом НЕ становятся.

    Без этой половины «выровнять в проценты» однажды применят предикатом по
    имени, и покрытие процентов уедет в 100 раз.

    ⚠️ Смотрим на ЕДИНИЦУ, а не на текст формулы. Каналов у ошибки два, и
    второй сильнее: `unit_kind: "percent"` в реестре перебивает вывод по
    структуре, так что «1,2 раза» подписывается процентом при нетронутой
    формуле. Пока условие читало только «* 100», такая правка проходила молча.
    """
    сотня = [
        f"{enum}: единица {unit!r} — {formula}"
        for enum, formula, unit in _расчётные(profile_id)
        if enum in РАЗЫ and ("* 100" in formula or "× 100" in formula or unit == "%")
    ]
    assert not сотня, f"{profile_id}: показатель «в разах» стал процентом — {сотня}"


def test_one_enum_is_measured_the_same_way_in_every_cell():
    """Один enum — одна единица во всех десяти ячейках.

    Ячейки правятся по одной, и разъезжаются они тоже по одной: ROA прожил так
    девять ячеек — процент в пяти нефинансовых, доля в четырёх банковских. По
    имени показателя не видно, какой отчёт принесли, поэтому читатель складывает
    в одну таблицу числа, отличающиеся в сто раз, и сравнивает их между собой.

    Проверка не по списку известных показателей, а ПО ВСЕМ расчётным: список
    пришлось бы пополнять руками, и следующий разъезд случился бы в строке,
    которую в него забыли внести.
    """
    единицы: dict[str, dict[str, list[str]]] = {}
    for cell in PROFILE_IDS:
        for enum, _formula, unit in _расчётные(cell):
            единицы.setdefault(enum, {}).setdefault(unit, []).append(cell)
    разъехались = {e: v for e, v in единицы.items() if len(v) > 1}
    assert not разъехались, (
        f"единица показателя зависит от ячейки: {разъехались} — "
        f"это то же самое, чем был ROA до 20.08"
    )


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_days_family_is_measured_in_days(profile_id):
    """Оборачиваемости и цикл — «дн.» В КАЖДОЙ ячейке, где они заведены.

    Поимённая проверка двух показателей в одной ячейке (как было до 21.08)
    оставляла остальные без охраны: снятие `unit_kind` у оборачиваемости
    дебиторки и запасов во всех пяти нефинансовых ячейках проходило зелёным.
    Сторож единообразия такую правку тоже пропускает — он ловит РАЗЪЕЗД
    ячеек, а когда все едут вместе, разъезда нет.
    """
    не_дни = [
        f"{enum}: единица {unit!r} — {formula}"
        for enum, formula, unit in _расчётные(profile_id)
        if enum in ДНИ and unit != "дн."
    ]
    assert not не_дни, f"{profile_id}: показатель «в днях» подписан не днями — {не_дни}"


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_a_hundred_in_the_formula_means_percent_and_only_percent(profile_id):
    """Сотня в формуле и процент в реестре — одно и то же, в обе стороны.

    ЗАЧЕМ. Единицу расчётной строки называют ДВА языка: реестровый помощник
    `unit_for_calculated` и подстрочная проверка `"* 100" in formula` у
    потребителей, которым `calculated_meta` недоступен, — сегодня это
    `skills/aifa/extractor.py` и `skills/aifa_baseline/extractor.py`, а до
    перевода боевого движка на помощник был и `two_stage_extractor.py`. Пока
    «сотня» и «процент» означают одно и то же, все они согласны с реестром без
    второго механизма подписи. Разойтись эти два языка могут в обе стороны, и
    обе молчаливы:

    · сотня без процента — деньги подписываются процентом. Именно поэтому
      компенсация потребителя записана ДЕЛЕНИЕМ:
      `OPERATING_EXPENSES_BANKS / (COST_TO_INCOME_RATIO / 100)`. Арифметически
      равное `... / COST_TO_INCOME_RATIO * 100` красит только регрессионный
      снимок формул — то есть ровно то, что человек штатно обновляет, правя
      формулу, — а операционный доход банка молча становится процентом;

    · процент без сотни — доля подписывается процентом (`unit_kind:
      "percent"` при нетронутой формуле): движок напечатает «0,25 %» там, где
      реестр обещает 25.
    """
    расхождения = []
    for enum, formula, unit in _расчётные(profile_id):
        сотня = "* 100" in formula or "× 100" in formula
        процент = unit == "%"
        if сотня != процент:
            расхождения.append(
                f"{enum}: сотня в формуле — {сотня}, единица реестра — {unit!r} ({formula})"
            )
    assert not расхождения, (
        f"{profile_id}: формула и единица говорят разное про сотню — {расхождения}"
    )


def test_unit_families_have_no_dead_members():
    """В наборах нет имён, которых в реестре уже нет.

    Мёртвый член — сторож, охраняющий пустоту: показатель переименовали,
    проверка осталась зелёной, а новое имя не охраняет никто.
    """
    живые = {enum for cell in PROFILE_IDS for enum, _f, _u in _расчётные(cell)}
    мёртвые = {
        "ПРОЦЕНТНОЕ_СЕМЕЙСТВО": sorted(ПРОЦЕНТНОЕ_СЕМЕЙСТВО - живые),
        "РАЗЫ": sorted(РАЗЫ - живые),
        "ДНИ": sorted(ДНИ - живые),
    }
    assert not any(мёртвые.values()), f"имён нет среди расчётных: {мёртвые}"


def test_cir_consumer_stays_money():
    """Там, где структура формулы врёт, слово реестра сильнее вывода."""
    bank = registry_for_profile("ifrs_bank_ru")
    b_meta, b_calc = bank["calculated_meta"], {e: f for e, _n, f, _d in bank["calculated"]}
    assert bx.unit_for_calculated(
        b_calc["OPERATING_INCOME_CIR"],
        ДЕНЕЖНАЯ_ПОДПИСЬ,
        b_meta.get("OPERATING_INCOME_CIR", {}).get("unit_kind", ""),
    ) == ДЕНЕЖНАЯ_ПОДПИСЬ


# ── 4. приведение к году ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [("FY 2024", 1.0), ("12M 2024", 1.0), ("9M 2024", 12 / 9), ("6M 2024", 2.0),
     ("3M 2024", 4.0), ("Q1 2024", 4.0), ("31.03.2024", 1.0), ("", 1.0)],
)
def test_annualization_factor(label, expected):
    assert bx.annualization_factor(label) == pytest.approx(expected)


def test_annualized_metrics_are_declared_in_bank_cells():
    """Текст называет NIM/CoR/рентабельности аннуализированными — код обязан приводить."""
    meta = registry_for_profile("ifrs_bank_ru")["calculated_meta"]
    for enum in ("NET_INTEREST_MARGIN", "COST_OF_RISK_GENERAL", "ROE", "ROA",
                 "ASSET_YIELD", "FUNDING_COST"):
        assert meta.get(enum, {}).get("annualize"), f"{enum} не приводится к году"


def test_annualization_doubles_half_year_ratio():
    """Полугодовой NIM 3,1% приводится к 6,3% годовых, годовой — не трогается."""
    assert bx.annualized(0.0315, "6M 2024", True) == pytest.approx(0.063)
    assert bx.annualized(0.0315, "FY 2024", True) == pytest.approx(0.0315)
    assert bx.annualized(0.0315, "6M 2024", False) == pytest.approx(0.0315)
    assert bx.annualized(None, "6M 2024", True) is None


def test_ratio_of_two_flows_is_not_annualized():
    """У Cost/Income множитель периода сокращается — приводить нечего.

    Если кто-то поставит ему `annualize`, полугодовой CIR станет 48% вместо 24%.
    """
    meta = registry_for_profile("ifrs_bank_ru")["calculated_meta"]
    assert not meta.get("COST_TO_INCOME_RATIO", {}).get("annualize")


# ── 5. диапазон смысла ───────────────────────────────────────────────────────


def test_inverted_cost_to_income_is_reported_not_swallowed():
    """Ровно то число, что видел пользователь на проде, обязано быть окликнуто."""
    note = sanity_warning("COST_TO_INCOME_RATIO", 4.099)
    assert note and "вне диапазона" in note
    assert sanity_warning("COST_TO_INCOME_RATIO", 0.244) is None


def test_cost_of_risk_release_is_normal_and_huge_release_is_not():
    """ПЕРЕВЁРНУТО ОСОЗНАННО (#151). Раньше здесь стояло «отрицательная стоимость
    риска — сбой», и это было верно ПРИ ТОЙ формуле: она брала ABS(изменение
    резерва), так что отрицательным результат мог стать только от поломки.

    Но ABS и был поломкой: реестр специально просит извлекать создание резерва со
    знаком минус, а восстановление со знаком плюс, — и ABS этот знак тут же
    выбрасывал. Банк, РАСПУСТИВШИЙ резервы, выглядел ровно как банк, доначисливший
    столько же. ABS снят, значит роспуск даёт отрицательную стоимость риска —
    законный и содержательный результат, окликать его нельзя.
    """
    assert sanity_warning("COST_OF_RISK_GENERAL", -0.01) is None, (
        "роспуск резервов — законная операция, а не сбой входов")
    assert sanity_warning("COST_OF_RISK_GENERAL", 0.008) is None
    assert sanity_warning("COST_OF_RISK_GENERAL", -0.5), (
        "роспуск в половину портфеля за период — это уже не результат, а входы")


def test_profitability_bounds_follow_the_unit():
    """ROE 24,6% — норма и долей, и процентами; 24,6 РАЗ — нет."""
    assert sanity_warning("ROE", 0.246) is None
    assert sanity_warning("ROE", 24.6, "%") is None
    assert sanity_warning("ROE", 24.6) is not None


def test_sanity_is_silent_on_unknown_and_non_numeric():
    assert sanity_warning("SALES_REVENUE", 10**9) is None
    assert sanity_warning("ROE", None) is None
    assert sanity_warning("ROE", "н/д") is None


def test_prompt_row_carries_the_warning():
    """Предупреждение доезжает до модели, а не остаётся в логе сервера."""
    from finengine.skills.reasoner.runner import build_metrics_table_str

    row = build_metrics_table_str([{
        "enum": "COST_TO_INCOME_RATIO", "name_ru": "Cost/Income",
        "value_current": 4.099, "value_prev": None, "unit": "",
        "sanity": sanity_warning("COST_TO_INCOME_RATIO", 4.099),
    }])
    assert "⚠" in row and "вне диапазона" in row


def test_prompt_row_shows_a_readable_number_without_money_unit():
    """«0.24632214874683647 … млн. руб.» — то, что видела модель до #146."""
    from finengine.skills.reasoner.runner import build_metrics_table_str

    row = build_metrics_table_str([{
        "enum": "ROE", "name_ru": "Рентабельность собственного капитала",
        "value_current": 0.24632214874683647, "value_prev": 0.0466, "unit": "",
    }])
    assert "0.2463" in row
    assert "24632214874683647" not in row
    assert "руб" not in row
