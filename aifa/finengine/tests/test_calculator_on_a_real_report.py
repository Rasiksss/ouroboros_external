"""Калькулятор на НАСТОЯЩЕЙ отчётности — НОВАТЭК, МСФО, 2021.

ЗАЧЕМ ЭТОТ ТЕСТ ЕСТЬ. Все остальные проверки расчёта гоняют выдуманные числа:
выручка 1000, прибыль 100. На таких данных не видно ни подстановки нуля, ни
цепочек зависимостей, ни расхождения между «своей» формулой и реестровой —
всё сходится, потому что складывать нечего.

Здесь числа сняты вручную с формы отчёта (стр. 10, 11, 13, 14) и сверены с ней
построчно, а ожидаемые значения посчитаны на бумаге. Расхождение тут означает,
что калькулятор поехал на настоящем документе, а не в тесте.

⚠️ ЧИСЛА В ОЖИДАНИЯХ НЕ ПРАВИТЬ ПОД ВЫВОД. Они проверяемы: открой
`tests/fixtures/novatek_2021_ifrs_nf_ru.json`, возьми входы формулы и посчитай.
Разошлось — сначала докажи, что права новая арифметика, и только потом меняй.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.skills.ratios import compute_ratios

ROOT = Path(__file__).resolve().parents[1]
ФИКСТУРА = ROOT / "tests" / "fixtures" / "novatek_2021_ifrs_nf_ru.json"
ЯЧЕЙКА, ТЕК, ПРЕД = "ifrs_nf_ru", "FY 2021", "FY 2020"


@pytest.fixture
def величины() -> list[dict]:
    return json.loads(ФИКСТУРА.read_text(encoding="utf-8"))["metrics"]


def _посчитать(величины: list[dict], **kw) -> dict[str, dict]:
    return {r["enum"]: r for r in compute_ratios(
        величины, ЯЧЕЙКА, period_current=ТЕК, period_prev=ПРЕД, **kw)}


# ── 1. Реестровые коэффициенты сходятся с ручным счётом ─────────────────────
#
# Каждое ожидание проверяемо по фикстуре:
#   EBITDA        = 279 046 (опер. прибыль) + 56 599 (износ)
#   TOTAL_DEBT    = 67 014 + 3 426 + 113 029 + 3 589
#   NET_DEBT      = 187 058 − 45 920 (деньги) − 60 177 (депозиты)
#   EBITDA_MARGIN = 335 645 / 1 156 724 × 100
#   CURRENT_RATIO = 577 067 / 388 783
ОЖИДАНИЯ = [
    ("EBITDA", 335_645.0, 1.0),
    ("TOTAL_DEBT", 187_058.0, 1.0),
    ("NET_DEBT", 80_961.0, 1.0),
    ("FREE_CASH_FLOW", 247_848.0, 1.0),
    ("EBITDA_MARGIN", 29.017, 0.01),
    ("EBIT_MARGIN", 24.124, 0.01),
    ("CURRENT_RATIO", 1.4843, 0.001),
    ("RATIO_NET_DEBT_EBITDA", 0.2412, 0.001),
    ("ACCOUNTS_RECEIVABLE_TURNOVER", 31.674, 0.01),
]


@pytest.mark.parametrize(("enum", "ждём", "допуск"), ОЖИДАНИЯ)
def test_registry_ratio_matches_the_hand_count(величины, enum, ждём, допуск):
    строка = _посчитать(величины).get(enum)
    assert строка is not None, f"{enum} не посчитан вовсе"
    got = строка.get("value_current")
    assert got is not None, f"{enum} посчитан пустым: {строка.get('calc_missing')}"
    assert abs(got - ждём) <= допуск, (
        f"{enum}: движок дал {got}, на бумаге {ждём}. Входы строки: "
        f"{[(i['enum'], i.get('value')) for i in строка.get('inputs') or []]}"
    )


# ── 2. `only` тянет за собой рассчитываемые входы ───────────────────────────
@pytest.mark.parametrize("enum", ["NET_DEBT", "FREE_CASH_FLOW"])
def test_asking_for_one_ratio_pulls_its_calculated_inputs(величины, enum):
    """🔴 ДЕФЕКТ 29.08.2026: «посчитай чистый долг» отдельной просьбой не работало.

    `NET_DEBT` стоит на `TOTAL_DEBT`, а тот сам рассчитываемый. `only` сужал
    состав буквально, поэтому вход не считался и чистый долг выходил ПУСТЫМ —
    при том что тот же расчёт без сужения давал 80 961.

    Хуже пустоты был диагноз: «TOTAL_DEBT — не заказывали (нужно извлечение)».
    Извлечение рассчитываемый показатель не добудет НИКОГДА, и модель по такому
    совету уходила переизвлекать документ на 6–25 минут за тем же результатом.
    """
    один = _посчитать(величины, only=[enum]).get(enum)
    все = _посчитать(величины).get(enum)
    assert один is not None and один.get("value_current") is not None, (
        f"--only {enum} вернул пусто, а без сужения считается "
        f"({все and все.get('value_current')}): замыкание по зависимостям сломано"
    )
    assert один["value_current"] == все["value_current"], (
        f"{enum} с сужением и без дал разные числа: "
        f"{один['value_current']} против {все['value_current']}"
    )


# ── 3. Своя формула сравнялась с реестровой ─────────────────────────────────
def test_own_formula_may_use_the_same_functions_as_the_registry(величины):
    """AVG и ANNUAL разрешены телом навыка — значит обязаны и приниматься.

    До 29.08.2026 приёмный контроль сверялся со списком вызовов, которые
    исполняет обходчик AST, а `AVG`/`ANNUAL` раскрываются ДО разбора и в тот
    список не входят по построению. Реестровые формулы с ними считались, а
    такая же формула МОДЕЛИ отвергалась.
    """
    from finengine.skills.ratios import own_calc_defs

    своя = [{"enum": "DSO_OWN", "name_ru": "Оборачиваемость дебиторки",
             "formula": "DAYS_IN_PERIOD / (SALES_REVENUE / AVG(ACCOUNTS_RECEIVABLE))",
             "deps": ["SALES_REVENUE", "ACCOUNTS_RECEIVABLE"], "unit_kind": "days"}]
    приняты, отказы = own_calc_defs(своя, set())
    assert not отказы, f"своя формула с AVG отвергнута: {отказы}"

    строка = _посчитать(величины, only=["DSO_OWN"], extra=приняты)["DSO_OWN"]
    # Та же величина есть в реестре под своим именем — числа обязаны совпасть.
    реестровая = _посчитать(величины)["ACCOUNTS_RECEIVABLE_TURNOVER"]
    assert abs(строка["value_current"] - реестровая["value_current"]) < 0.01, (
        f"своя формула дала {строка['value_current']}, реестровая — "
        f"{реестровая['value_current']}: считают по-разному"
    )


def test_a_made_up_function_is_still_refused():
    """Отрицательный контроль: приём не должен принимать что попало."""
    from finengine.skills.ratios import own_calc_defs

    плохая = [{"enum": "BAD", "name_ru": "из SQL",
               "formula": "COALESCE(LONG_TERM_DEBT, 0) / SALES_REVENUE",
               "deps": ["LONG_TERM_DEBT", "SALES_REVENUE"], "unit_kind": "ratio"}]
    _, отказы = own_calc_defs(плохая, set())
    assert отказы and "COALESCE" in отказы[0], (
        "выдуманная функция принята — приёмный контроль перестал различать"
    )


# ── 4. 🔴 ЗНАЕТ ЛИ ЧЕЛОВЕК, ЧТО ВХОД ПОДСТАВЛЕН НУЛЁМ (Р14) ─────────────────
def test_a_missing_input_is_visible_in_the_row(величины):
    """Убрал слагаемое — число изменилось на 67 млрд, и об этом надо ЗНАТЬ.

    Подстановка нуля умышленна: `TOTAL_DEBT` в `_PARTIAL_ZERO_FORMULA_ENUMS`,
    и у компании может не быть аренды. Проверяем не её, а СЛЕД: признак
    `substituted_zero` обязан оставаться в строке, иначе неверное число
    неотличимо от верного.

    ⚠️ ЭТО НЕ ПОЛНАЯ ЗАЩИТА. До сводки в stdout и до модели признак не доходит —
    Р14 в бэклоге. Здесь стережём хотя бы то, что информация не исчезла.
    """
    без = [m for m in величины if m["enum"] != "LONG_TERM_DEBT"]
    строка = _посчитать(без, only=["TOTAL_DEBT"])["TOTAL_DEBT"]
    подставлено = [i["enum"] for i in строка.get("inputs") or []
                   if i.get("substituted_zero")]
    assert "LONG_TERM_DEBT" in подставлено, (
        "вход пропал, а следа подстановки нуля в строке нет: неверное число "
        f"({строка.get('value_current')} вместо 187 058) выглядит верным"
    )
