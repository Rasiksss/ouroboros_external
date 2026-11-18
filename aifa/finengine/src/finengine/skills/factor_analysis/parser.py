"""Excel parser for the factor analysis skill.

Reads plan/fact/nomenclature xlsx files produced by the synthetic
generator (and structurally similar real exports from 1C ERP / ТМХ
Бюджетирование).

Возвращает нормализованные DataFrame'ы — на этом этапе сырые цифры,
никакой математики факторов. Это критично для проверки качества парсинга
отдельно от методологии.

Архитектура парсинга:
    1. Каждый xlsx может содержать ≥2 листов (сводный + детализация по
       статьям). Парсер опознаёт лист по сигнатуре заголовков, не по
       индексу — иначе сломаемся на reorder'е листов.
    2. Шапка ищется по маркеру "Код" в первом столбце.
    3. Числа допускают строковую запись с пробелами/запятыми → `_to_num`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Сигнатуры заголовков — по ним опознаём тип листа.
# ──────────────────────────────────────────────────────────────────────
_PERIOD_HEADERS = {"код", "продукт", "ед. изм.", "количество", "цена за ед., ₽",
                   "выручка, ₽", "себестоимость, ₽", "маржа, ₽", "маржа, %"}
_COST_HEADERS = {"код статьи", "статья себестоимости"}
_NOMENCLATURE_HEADERS = {"код продукта", "наименование", "ед. изм.", "категория",
                         "плановая отпускная цена, ₽", "норматив гарантии, ₽/ед."}


@dataclass
class PeriodTable:
    """Сводный лист периода (План или Факт) — продукт × агрегаты."""
    label: str                      # "ПЛАН" | "ФАКТ"
    products: list[dict[str, Any]]  # [{code, name, unit, qty, price, revenue, cost_per_unit_total, ...}, ...]


@dataclass
class CostTable:
    """Детальный лист себестоимости — статья × продукт (на единицу)."""
    label: str
    cost_per_unit: dict[str, dict[str, float]]
    # cost_per_unit[product_code][cost_item_code] = value, ₽/ед.


@dataclass
class Nomenclature:
    products: list[dict[str, Any]]   # [{code, name, unit, category, plan_price, warranty_norm}, ...]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _to_num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _norm_header(s: Any) -> str:
    return str(s or "").strip().lower()


def _find_header_row(ws, expected: set[str], scan_rows: int = 30) -> int | None:
    """Find the row index (1-based) whose cell values cover at least 60% of
    `expected`. Returns None if not found within scan_rows."""
    expected_lower = {h.lower() for h in expected}
    for r in range(1, scan_rows + 1):
        row_vals = [_norm_header(c.value) for c in ws[r]]
        row_set = set(row_vals)
        if not row_set:
            continue
        overlap = expected_lower & row_set
        if len(overlap) >= max(2, int(len(expected_lower) * 0.6)):
            return r
    return None


def _build_col_map(ws, header_row: int) -> dict[str, int]:
    """Return {normalized_header: column_index (1-based)}."""
    return {_norm_header(c.value): c.column for c in ws[header_row] if c.value}


# ──────────────────────────────────────────────────────────────────────
# Sheet parsers
# ──────────────────────────────────────────────────────────────────────

def _parse_period_sheet(ws, label: str) -> PeriodTable | None:
    hr = _find_header_row(ws, _PERIOD_HEADERS)
    if hr is None:
        return None
    cmap = _build_col_map(ws, hr)

    def col(name: str) -> int | None:
        return cmap.get(name.lower())

    products: list[dict[str, Any]] = []
    for r in range(hr + 1, ws.max_row + 1):
        code = ws.cell(r, col("Код") or 1).value
        if not code or str(code).strip().lower() in {"итого", "total", ""}:
            continue
        row = {
            "code": str(code).strip(),
            "name": ws.cell(r, col("Продукт") or 2).value,
            "unit": ws.cell(r, col("Ед. изм.") or 3).value,
            "qty": _to_num(ws.cell(r, col("Количество") or 4).value),
            "price": _to_num(ws.cell(r, col("Цена за ед., ₽") or 5).value),
            "revenue": _to_num(ws.cell(r, col("Выручка, ₽") or 6).value),
            "cost_total": _to_num(ws.cell(r, col("Себестоимость, ₽") or 7).value),
        }
        if row["qty"] is None or row["price"] is None:
            continue
        products.append(row)

    return PeriodTable(label=label, products=products)


def _parse_cost_sheet(ws, label: str) -> CostTable | None:
    hr = _find_header_row(ws, _COST_HEADERS, scan_rows=8)
    if hr is None:
        return None

    # Header row: A=Код статьи, B=Статья, C..N = продукты (по названиям).
    # Map column index → product code via fuzzy name match later;
    # but для синтетики имена совпадают с PRODUCTS[].name.
    cmap = _build_col_map(ws, hr)
    # Все продуктовые колонки — это всё, что не "Код статьи" / "Статья".
    product_cols: dict[int, str] = {}
    for c in ws[hr]:
        name = _norm_header(c.value)
        if not name or name in {"код статьи", "статья себестоимости"}:
            continue
        product_cols[c.column] = str(c.value).strip()

    # Чтобы сшить с кодами продуктов из PeriodTable — оставляем имя как ключ
    # и применяем маппинг наверху (analyzer передаст список продуктов).
    cost_per_unit_by_name: dict[str, dict[str, float]] = {n: {} for n in product_cols.values()}

    for r in range(hr + 1, ws.max_row + 1):
        item_code = ws.cell(r, cmap.get("код статьи", 1)).value
        if not item_code or str(item_code).strip().upper() == "TOTAL":
            continue
        item_code = str(item_code).strip()
        for col_idx, pname in product_cols.items():
            v = _to_num(ws.cell(r, col_idx).value)
            if v is not None:
                cost_per_unit_by_name[pname][item_code] = v

    return CostTable(label=label, cost_per_unit=cost_per_unit_by_name)  # keyed by NAME


def _parse_nomenclature(ws) -> Nomenclature | None:
    hr = _find_header_row(ws, _NOMENCLATURE_HEADERS, scan_rows=10)
    if hr is None:
        return None
    cmap = _build_col_map(ws, hr)
    products: list[dict[str, Any]] = []
    for r in range(hr + 1, ws.max_row + 1):
        code = ws.cell(r, cmap.get("код продукта", 1)).value
        if not code:
            continue
        products.append({
            "code": str(code).strip(),
            "name": ws.cell(r, cmap.get("наименование", 2)).value,
            "unit": ws.cell(r, cmap.get("ед. изм.", 3)).value,
            "category": ws.cell(r, cmap.get("категория", 4)).value,
            "plan_price": _to_num(ws.cell(r, cmap.get("плановая отпускная цена, ₽", 5)).value),
            "warranty_norm": _to_num(ws.cell(r, cmap.get("норматив гарантии, ₽/ед.", 6)).value),
        })
    return Nomenclature(products=products)


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def parse_period_workbook(path: Path | str, label: str) -> tuple[PeriodTable, CostTable]:
    """Open xlsx and return (sводный, детализация себестоимости).
    Raises if either sheet not detected.
    """
    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True)
    period: PeriodTable | None = None
    cost: CostTable | None = None
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if period is None:
            p = _parse_period_sheet(ws, label)
            if p is not None and p.products:
                period = p
                continue
        if cost is None:
            c = _parse_cost_sheet(ws, label)
            if c is not None and c.cost_per_unit:
                cost = c
                continue
    if period is None:
        raise ValueError(f"{path.name}: сводный лист периода не найден")
    if cost is None:
        raise ValueError(f"{path.name}: лист детализации себестоимости не найден")
    # Сшиваем cost (keyed by name) → cost (keyed by code) для удобства downstream
    name_to_code = {p["name"]: p["code"] for p in period.products}
    cost.cost_per_unit = {
        name_to_code.get(name, name): items
        for name, items in cost.cost_per_unit.items()
    }
    return period, cost


def parse_nomenclature(path: Path | str) -> Nomenclature:
    path = Path(path)
    wb = openpyxl.load_workbook(path, data_only=True)
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        n = _parse_nomenclature(ws)
        if n is not None and n.products:
            return n
    raise ValueError(f"{path.name}: справочник номенклатуры не найден")
