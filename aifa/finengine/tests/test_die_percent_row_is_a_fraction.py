"""Строка «%» раскладки ДИЭ не умножается на сотню дважды.

ЧТО СЛОМАЛОСЬ. Реестр перевёл семейство доходностей из долей в проценты
(ROE = 24,6 вместо 0,246). У раскладки ДИЭ по банку СЕМЬ строк с
`kind: "metric"` и `is_percent: true`, и шесть из них — ровно те enum'ы,
которым правка дала сотню. Оба рендерера ДИЭ ждут ДОЛЮ и множат её на 100
сами:

* `web/src/lib/formatted-table.ts:76` — `${(value * 100)…}%`, прямо под
  комментарием «Проценты бэкенд отдаёт долей (0.12)»;
* `roles/formatter.py` (`_write_die_sheet`) — `cell.number_format = "0%"`,
  Excel множит сам.

Компенсация в `_die_table` стояла ТОЛЬКО в ветке `kind in {"margin",
"percent_of_revenue"}` (там маржи лежали процентами и до правки). Ветка
`kind == "metric"` отдавала значение как есть — и работало это ровно потому,
что шесть enum'ов были долями. На экране владельца получилось «ROE 2460 %»,
«CIR 2440 %», «LDR 9500 %», и ни один тест не покраснел: все они меряют
арифметику формулы, а не то, что видит человек.

ЧТО СТАЛО. `_die_fraction_for_percent_row` делит на 100, когда единица
значения — «%». Различитель — ЕДИНИЦА, а не список enum'ов: список стал бы
ещё одним местом, где живёт знание о единице показателя, и разъехался бы с
реестром (этим уже болеет `data/metrics.json`).

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `_die_fraction_for_percent_row` тело
`return value` — красен `test_percent_metric_rows_reach_the_renderer_as_a_fraction`
(6 строк из 6).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.roles.formatter import build_formatted_table
from finengine.roles.scope import build_role_plan
from finengine.skills import bulk_extractor as bx
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

LAYOUTS = json.loads(
    (Path(__file__).resolve().parents[1] / "src" / "finengine" / "roles" / "layouts.json").read_text(
        encoding="utf-8"
    )
)
BANK_CELLS = [p for p in PROFILE_IDS if "_bank_" in p]

# Значения взяты процентами — так их отдаёт реестр после правки 21.08.
ПРОЦЕНТНЫЕ_СТРОКИ_БАНКА = {
    "LOANS_TO_DEPOSITS": 95.0,
    "DIVIDEND_PAYOUT_RATIO": 50.0,
    "NET_INTEREST_MARGIN": 6.31,
    "COST_OF_RISK_GENERAL": 0.8,
    "COST_TO_INCOME_RATIO": 24.4,
    "ROE": 24.6,
}

# ⚠️ ЧУЖОЙ ДЕФЕКТ, НЕ ЭТОЙ ПРАВКИ. `die/bank` rows[13] подписан «CET1, %» и
# помечен `is_percent`, но enum за ним — COMMON_EQUITY_TIER_ONE_CAPITAL,
# то есть АБСОЛЮТНАЯ величина капитала в деньгах: во всех пяти банковских
# ячейках он лежит в `extractable`, а подсказка `ind_gaap_bank_en` прямо
# говорит «Take the absolute CET1 capital amount, not the CET1 ratio
# percentage». Норматив достаточности — отдельный enum `CET1_RATIO_DISCLOSED`,
# он тоже извлекаемый и уже в процентах. Строка печатала «123 456 700 %» и до
# перевода доходностей в проценты; чинится сменой enum'а в раскладке, а это
# меняет смысл строки на экране — решение владельца, не правка единицы.
# ⚠️ ПОЧИНЕНО 21.08.2026 — набор пуст, и пустым обязан остаться. Строка
# «CET1, %» переведена на `CET1_RATIO_DISCLOSED` (норматив достаточности,
# извлекается из примечания уже в процентах), а сам показатель — вместе с
# `TIER1_RATIO_DISCLOSED` — получил в реестре пяти банковских ячеек честную
# единицу «%»: подсказка говорила «Единица — проценты», а поле `unit` молчало,
# и компенсация раскладки не срабатывала. Разметка подтверждает форму:
# 163 строки `CET1_RATIO_DISCLOSED` в golden несут единицу «%» при значениях
# вида 15.7. Появилось имя в этом наборе — значит завели новую такую строку.
ИЗВЕСТНЫЙ_ДЕФЕКТ_РАСКЛАДКИ: set[str] = set()

META = {"period_current": "2025", "unit": "млн руб.", "company_type": "bank", "company_name": "Банк"}


def _metric_row(enum: str, value: float, unit: str) -> dict:
    return {
        "enum": enum, "name_ru": enum, "value_current": value, "unit": unit,
        "is_calculated": True, "found": True,
        "period_values": [{"period": "2025", "value": value, "unit": unit}],
    }


def _die_bank_rows(metrics: list[dict]) -> dict[str, dict]:
    plan = build_role_plan("die", "bank")
    assert plan.config, "предпосылка ушла: у роли ДИЭ по банку была раскладка"
    table = build_formatted_table(plan=plan, metrics=metrics, meta=META)
    return {
        str(row.get("enum")): row
        for row in table["rows"]
        if row.get("die_kind") == "metric" and row.get("enum")
    }


def _is_percent_metric_rows() -> list[dict]:
    rows = ((LAYOUTS["roles"]["die"]["formats"]["bank"]) or {}).get("rows") or []
    return [r for r in rows if r.get("kind") == "metric" and r.get("is_percent")]


def test_the_layout_still_has_percent_metric_rows():
    """Предпосылка: строки `kind=metric` + `is_percent` в раскладке есть."""
    enums = {str(r.get("enum")) for r in _is_percent_metric_rows()}
    assert enums >= set(ПРОЦЕНТНЫЕ_СТРОКИ_БАНКА), (
        f"раскладка ДИЭ/банк изменилась: строк-процентов {sorted(enums)}, "
        f"сторож ждал {sorted(ПРОЦЕНТНЫЕ_СТРОКИ_БАНКА)}"
    )


def test_percent_metric_rows_reach_the_renderer_as_a_fraction():
    """Бэкенд отдаёт долю — рендерер вернёт человеку исходный процент."""
    metrics = [_metric_row(e, v, "%") for e, v in ПРОЦЕНТНЫЕ_СТРОКИ_БАНКА.items()]
    rows = _die_bank_rows(metrics)
    сотня_дважды = []
    for enum, процент in ПРОЦЕНТНЫЕ_СТРОКИ_БАНКА.items():
        значение = rows[enum]["values"]["2025"]
        # `* 100` — ровно то, что сделают оба рендерера ДИЭ.
        if значение * 100 != pytest.approx(процент):
            сотня_дважды.append(
                f"{enum}: реестр дал {процент}, бэкенд отдал {значение}, "
                f"на экране будет {значение * 100:.2f}%"
            )
    assert not сотня_дважды, f"процент умножается на сотню дважды — {сотня_дважды}"


def test_a_fraction_stays_a_fraction():
    """Значение БЕЗ процентной единицы делению не подлежит.

    Обратная половина: делить всё подряд значило бы починить сегодняшний
    реестр и сломать любой источник, который отдаёт долю (0,246).
    """
    rows = _die_bank_rows([_metric_row("ROE", 0.246, "")])
    assert rows["ROE"]["values"]["2025"] == pytest.approx(0.246)


def test_percent_rows_point_at_percent_metrics():
    """Строка «%» раскладки обязана указывать на процентный показатель.

    Иначе перевод единицы в реестре не спасёт: строка `is_percent` над
    денежным enum'ом печатает деньги процентом при любой компенсации.
    """
    процентные = {
        enum
        for cell in BANK_CELLS
        for enum, _name, formula, _deps in registry_for_profile(cell)["calculated"]
        if bx.unit_for_calculated(
            formula,
            "млн. руб.",
            registry_for_profile(cell)["calculated_meta"]
            .get(str(enum).strip().upper(), {})
            .get("unit_kind", ""),
        )
        == "%"
    }
    # Процентным показатель делает ЛИБО формула с сотней (расчётный), ЛИБО
    # объявленная единица реестра (извлекаемый: норматив достаточности лежит
    # в отчёте уже процентом). Считать только расчётные значило бы объявить
    # дефектом любую верную строку над извлечённым процентом.
    # ⚠️ ТРЕБУЕМ «%» ВО ВСЕХ ЯЧЕЙКАХ, ГДЕ ПОКАЗАТЕЛЬ ЕСТЬ, а не хотя бы в одной.
    # Объединение было первой редакцией, и мутация её опрокинула: единица снята
    # у CET1 в одной ячейке из пяти — этот сторож зелёный, и сторож единообразия
    # реестра тоже зелёный (он смотрит только `calculated`). Ячейка, забывшая
    # единицу, печатала бы 1570 % при четырёх верных соседях.
    объявили: dict[str, list[bool]] = {}
    for cell in BANK_CELLS:
        for m in registry_for_profile(cell)["extractable"]:
            объявили.setdefault(str(m["enum"]), []).append(
                str(m.get("unit") or "").strip() == "%")
    процентные |= {enum for enum, flags in объявили.items() if all(flags)}
    чужие = {
        str(r.get("enum")) for r in _is_percent_metric_rows()
    } - процентные
    assert чужие == ИЗВЕСТНЫЙ_ДЕФЕКТ_РАСКЛАДКИ, (
        f"строки «%» ДИЭ/банк над не-процентными показателями: {sorted(чужие)}; "
        f"известен и разобран в шапке файла только {sorted(ИЗВЕСТНЫЙ_ДЕФЕКТ_РАСКЛАДКИ)}"
    )
