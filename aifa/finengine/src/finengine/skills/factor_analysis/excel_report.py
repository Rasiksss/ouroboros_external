"""Multi-sheet xlsx report для факторного анализа.

Структура:
    1. Итоги — план/факт/Δ по revenue/cost/margin + 3 фактора маржи + per-product summary
    2. Факторы_продукты — Δ маржи по каждому продукту с разложением на 3 фактора
    3. Себестоимость_статьи — статья × продукт: Δ COGS, объёмный эффект, эффект ставки
    4. Исходные_данные — что прочитано из xlsx план/факт/номенклатуры
    5. Методика — формулы и проверки

Намеренно повторяет UX, к которому пользователь привык от ChatGPT Code
Interpreter — заказчик будет открывать файл и листать вкладки.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


# ────────────────────── styles ──────────────────────
HDR_FILL = PatternFill("solid", fgColor="FFD9E1F2")
TOTAL_FILL = PatternFill("solid", fgColor="FFFFE699")
HDR_FONT = Font(bold=True)
ITAL = Font(italic=True, color="888888")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
RIGHT = Alignment(horizontal="right")

# Формат «#,##0.0 "млн ₽"»  с принудительной 1 десятой и явным суффиксом.
# Это убирает двусмысленность «270,216 — это 270 тысяч или 270 млн?»
MLN_FMT = '#,##0.0" млн ₽";-#,##0.0" млн ₽";0.0" млн ₽"'
PCT_FMT = '+0.00%;-0.00%;0.00%'

# Человеко-читаемые названия методов для колонки «Метод» (вместо chain/laspeyres).
METHOD_RU = {
    "chain": "цепная подстановка (Q→S→P)",
    "margin_chain": "цепная по марже/ед.",
    "laspeyres": "Δнорма + Δобъём (Laspeyres)",
    "absolute": "Факт − План (без декомпозиции)",
}


def _autosize(ws, max_w: int = 38) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        ml = 0
        for c in col:
            if c.value is not None:
                ml = max(ml, len(str(c.value)))
        ws.column_dimensions[letter].width = min(max(12, ml + 2), max_w)



def _set_mln(cell, value_rubles: float) -> None:
    """Записать значение в ₽ как «X.X млн ₽» через числовой формат."""
    cell.value = round(value_rubles / 1_000_000, 2)
    cell.number_format = MLN_FMT
    cell.alignment = RIGHT


def _hdr_row(ws, row: int, headers: list[str]) -> None:
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = CENTER


# ────────────────────── sheet writers ──────────────────────

def _write_totals(ws, report: dict[str, Any]) -> None:
    ws.title = "Итоги"
    ws["A1"] = (
        f"Факторный анализ {report.get('period_label') or ''} — итоговый отчёт"
    )
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:E1")
    ws["A2"] = (
        f"План vs Факт. Все суммы в млн ₽ (если не указано иначе). "
        f"Источники: {report.get('plan_filename', '')} / "
        f"{report.get('fact_filename', '')}."
    )
    ws["A2"].font = ITAL
    ws.merge_cells("A2:E2")

    # Aggregates table
    _hdr_row(ws, 4, ["Показатель", "План", "Факт", "Δ", "Δ %"])
    agg = report["aggregates"]
    for r_idx, (label, key) in enumerate(
        [("Выручка", "revenue"), ("Себестоимость", "cost"), ("Маржа", "margin")],
        start=5,
    ):
        ws.cell(row=r_idx, column=1, value=label).font = HDR_FONT
        _set_mln(ws.cell(row=r_idx, column=2), agg[key]["plan"])
        _set_mln(ws.cell(row=r_idx, column=3), agg[key]["fact"])
        _set_mln(ws.cell(row=r_idx, column=4), agg[key]["delta"])
        if "delta_pct" in agg[key]:
            c = ws.cell(row=r_idx, column=5, value=agg[key]["delta_pct"] / 100)
            c.number_format = PCT_FMT
            c.alignment = RIGHT

    # Margin 3-factor table
    margin_factors = [f for f in report["factors"] if f["group"] == "margin"]
    if margin_factors:
        start = 10
        ws.cell(row=start, column=1, value="Разложение Δ маржи (3 фактора)").font = HDR_FONT
        _hdr_row(ws, start + 1, ["Фактор", "Эффект", "Формула"])
        for i, f in enumerate(margin_factors, start=start + 2):
            ws.cell(row=i, column=1, value=f["name"])
            _set_mln(ws.cell(row=i, column=2), f["value"])
            ws.cell(row=i, column=3, value=f.get("formula", "")).font = ITAL
        total_row = start + 2 + len(margin_factors)
        ws.cell(row=total_row, column=1, value="ИТОГО Δ маржи").font = HDR_FONT
        c = ws.cell(row=total_row, column=2)
        _set_mln(c, sum(f["value"] for f in margin_factors))
        c.font = HDR_FONT
        c.fill = TOTAL_FILL

    # 9-factor analytical view
    analytic = [f for f in report["factors"] if f["group"] != "margin"]
    if analytic:
        start = 18
        ws.cell(row=start, column=1, value="Детализация (9 факторов — Revenue + Cost)").font = HDR_FONT
        _hdr_row(ws, start + 1, ["Фактор", "Группа", "Эффект", "Метод"])
        for i, f in enumerate(analytic, start=start + 2):
            ws.cell(row=i, column=1, value=f["name"])
            ws.cell(row=i, column=2, value=f["group"])
            _set_mln(ws.cell(row=i, column=3), f["value"])
            ws.cell(row=i, column=4,
                    value=METHOD_RU.get(f.get("method", ""), f.get("method", ""))).font = ITAL

    _autosize(ws)


def _write_product_factors(wb, report: dict[str, Any]) -> None:
    ws = wb.create_sheet("Факторы_продукты")
    ws["A1"] = "Δ маржи по каждому продукту (разложение на 3 фактора)"
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:H1")

    _hdr_row(ws, 3, [
        "Код", "Продукт",
        "План маржа", "Факт маржа", "Δ маржи",
        "Объём/структура", "Цена", "Себестоимость/ед.",
    ])

    products = report.get("products_breakdown", [])
    margin_factors = {f["id"]: f for f in report["factors"] if f["group"] == "margin"}
    vs_byp = (margin_factors.get("margin_volume_struct") or {}).get("by_product") or {}
    pr_byp = (margin_factors.get("margin_price") or {}).get("by_product") or {}
    uc_byp = (margin_factors.get("margin_unit_cost") or {}).get("by_product") or {}

    for r_idx, p in enumerate(products, start=4):
        code = p.get("code", "")
        ws.cell(row=r_idx, column=1, value=code)
        ws.cell(row=r_idx, column=2, value=p.get("name", ""))
        _set_mln(ws.cell(row=r_idx, column=3), p.get("margin_plan") or 0)
        _set_mln(ws.cell(row=r_idx, column=4), p.get("margin_fact") or 0)
        _set_mln(ws.cell(row=r_idx, column=5), p.get("margin_delta") or 0)
        _set_mln(ws.cell(row=r_idx, column=6), vs_byp.get(code, 0))
        _set_mln(ws.cell(row=r_idx, column=7), pr_byp.get(code, 0))
        _set_mln(ws.cell(row=r_idx, column=8), uc_byp.get(code, 0))

    # Totals row — SUM-формула вернёт уже-в-млн числа, потому формат тот же.
    total_row = 4 + len(products)
    ws.cell(row=total_row, column=1, value="TOTAL").font = HDR_FONT
    ws.cell(row=total_row, column=2, value="Итого").font = HDR_FONT
    for col in (3, 4, 5, 6, 7, 8):
        col_letter = get_column_letter(col)
        c = ws.cell(row=total_row, column=col,
                    value=f"=SUM({col_letter}4:{col_letter}{total_row - 1})")
        c.number_format = MLN_FMT
        c.fill = TOTAL_FILL
        c.font = HDR_FONT
        c.alignment = RIGHT

    _autosize(ws)


def _write_cost_items(wb, report: dict[str, Any]) -> None:
    ws = wb.create_sheet("Себестоимость_статьи")
    ws["A1"] = "Разложение Δ себестоимости по статьям × продукт"
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:G1")

    cost_factors = [f for f in report["factors"]
                    if f["group"] == "cost" and f.get("by_product")]
    if not cost_factors:
        ws["A3"] = "(нет данных по статьям себестоимости в этом scope)"
        return

    # Per-product формула: (c_F − c_P) × Q_F = … для конкретного продукта,
    # а не повторяющаяся агрегатная сумма. Источники по qty —
    # products_breakdown[*].qty_plan / qty_fact, по тарифам — report.plan_
    # unit_costs / fact_unit_costs[factor_id].
    by_code_product = {p["code"]: p for p in report.get("products_breakdown", [])}
    plan_uc = report.get("plan_unit_costs") or {}
    fact_uc = report.get("fact_unit_costs") or {}

    _hdr_row(ws, 3, [
        "Статья", "Код продукта", "План тариф", "Факт тариф",
        "Δ статьи", "Метод", "Формула"
    ])
    row = 4
    for f in cost_factors:
        fid = f["id"]
        pc_p = plan_uc.get(fid) or {}
        pc_f = fact_uc.get(fid) or {}
        for code, val in (f.get("by_product") or {}).items():
            cP = pc_p.get(code, 0)
            cF = pc_f.get(code, 0)
            qP = (by_code_product.get(code) or {}).get("qty_plan") or 0
            qF = (by_code_product.get(code) or {}).get("qty_fact") or 0
            # Per-product формула: Δ_по_продукту = Q_F·c_F − Q_P·c_P,
            # либо удобнее показать «(c_F−c_P)·Q_F + c_P·(Q_F−Q_P)» (Laspeyres
            # двухуровневая). Сжатый вариант для xlsx: (Q₁·c₁ − Q₀·c₀).
            if f.get("method") == "laspeyres":
                d_norm = (cF - cP) * qF
                d_vol = cP * (qF - qP)
                formula_text = (
                    f"Δ_норма + Δ_объём = "
                    f"({(cF - cP)/1e6:+.2f} млн ₽)×{qF} + "
                    f"({cP/1e6:.2f} млн ₽)×({qF - qP:+}) = "
                    f"{d_norm/1e6:+.2f} + {d_vol/1e6:+.2f} = "
                    f"{(d_norm + d_vol)/1e6:+.2f} млн ₽"
                )
            else:
                formula_text = (
                    f"Q₁·c₁ − Q₀·c₀ = {qF}×{cF/1e6:.2f} − {qP}×{cP/1e6:.2f} = "
                    f"{val/1e6:+.2f} млн ₽"
                )
            ws.cell(row=row, column=1, value=f["name"])
            ws.cell(row=row, column=2, value=code)
            _set_mln(ws.cell(row=row, column=3), cP)
            _set_mln(ws.cell(row=row, column=4), cF)
            _set_mln(ws.cell(row=row, column=5), val)
            ws.cell(row=row, column=6,
                    value=METHOD_RU.get(f.get("method", ""), f.get("method", ""))).font = ITAL
            ws.cell(row=row, column=7, value=formula_text).font = ITAL
            row += 1

    _autosize(ws, max_w=60)


def _write_source_data(wb, report: dict[str, Any]) -> None:
    ws = wb.create_sheet("Исходные_данные")
    ws["A1"] = "Что было прочитано из xlsx (план / факт)"
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:H1")

    _hdr_row(ws, 3, [
        "Код", "Продукт",
        "План кол-во", "Факт кол-во",
        "План цена/ед", "Факт цена/ед",
        "План выручка", "Факт выручка",
    ])
    for r_idx, p in enumerate(report.get("products_breakdown", []), start=4):
        ws.cell(row=r_idx, column=1, value=p.get("code"))
        ws.cell(row=r_idx, column=2, value=p.get("name"))
        ws.cell(row=r_idx, column=3, value=p.get("qty_plan"))
        ws.cell(row=r_idx, column=4, value=p.get("qty_fact"))
        _set_mln(ws.cell(row=r_idx, column=5), p.get("price_plan") or 0)
        _set_mln(ws.cell(row=r_idx, column=6), p.get("price_fact") or 0)
        _set_mln(ws.cell(row=r_idx, column=7), p.get("revenue_plan") or 0)
        _set_mln(ws.cell(row=r_idx, column=8), p.get("revenue_fact") or 0)

    _autosize(ws)


def _write_methodology(wb, report: dict[str, Any]) -> None:
    ws = wb.create_sheet("Методика")
    ws["A1"] = "Методика расчёта факторного анализа"
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:C1")
    ws["A2"] = (
        "Все формулы применяются точно, без остатка. "
        "Аддитивность проверяется: Σ факторов = Δ агрегата."
    )
    ws["A2"].font = ITAL
    ws.merge_cells("A2:C2")

    _hdr_row(ws, 4, ["Показатель", "Формула", "Смысл"])
    rows = [
        ("Плановая маржа", "Σ Q₀ × (P₀ − C₀)", "Плановый объём × плановая маржа/ед."),
        ("Фактическая маржа", "Σ Q₁ × (P₁ − C₁)", "Фактический объём × фактическая маржа/ед."),
        ("Эффект объёма/структуры", "Σ (Q₁ − Q₀) × (P₀ − C₀)",
         "Вклад изменения количества и микса по плановым маржам/ед."),
        ("Эффект цены", "Σ Q₁ × (P₁ − P₀)",
         "Вклад изменения цены реализации на фактическом объёме."),
        ("Эффект себестоимости/ед.", "−Σ Q₁ × (C₁ − C₀)",
         "Вклад изменения удельной себестоимости (знак отрицательный — рост C ухудшает маржу)."),
        ("Проверка аддитивности", "Δ маржи = объём/структура + цена + себестоимость/ед.",
         "Разложение сходится точно при идентичных границах номенклатуры."),
        ("9-факторная аналитика", "Revenue: Q→S→P (chain) + Cost: Δнорма+Δобъём (Laspeyres)",
         "Альтернативный взгляд: декомпозиция Revenue и Cost отдельно (8-9 факторов)."),
    ]
    for r_idx, (a, b, c) in enumerate(rows, start=5):
        ws.cell(row=r_idx, column=1, value=a).font = HDR_FONT
        ws.cell(row=r_idx, column=2, value=b)
        ws.cell(row=r_idx, column=3, value=c).alignment = Alignment(wrap_text=True)

    # Notes from report itself
    base = 5 + len(rows) + 2
    ws.cell(row=base, column=1, value="Журнал расчёта (method_notes)").font = HDR_FONT
    for r_idx, n in enumerate(report.get("method_notes", []), start=base + 1):
        ws.cell(row=r_idx, column=1, value=f"• {n}")
        ws.merge_cells(start_row=r_idx, start_column=1, end_row=r_idx, end_column=3)

    _autosize(ws)


# ────────────────────── public API ──────────────────────

def build_report_xlsx(report: dict[str, Any], output_path: Path | str) -> Path:
    """Write multi-sheet xlsx report at `output_path` and return Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    _write_totals(wb.active, report)
    _write_product_factors(wb, report)
    _write_cost_items(wb, report)
    _write_source_data(wb, report)
    _write_methodology(wb, report)
    wb.save(output_path)
    return output_path
