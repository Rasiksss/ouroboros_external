"""High-level orchestrator: parse files → resolve scope → compute factors →
build a structured Report ready for LLM commentary.

Используется тулом `factor_analysis` из llm/tools.py. Reentrant — без
глобального состояния.
"""
from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

from .parser import (
    parse_period_workbook, parse_nomenclature,
    PeriodTable, CostTable, Nomenclature,
)
from .methodology import (
    FactorResult, revenue_decomposition,
    cost_item_decomposition, cost_item_absolute,
    margin_decomposition,
)

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent / "config"


@dataclass
class Report:
    """Структурированный результат факторного анализа."""
    period_label: str
    scope: dict[str, Any]
    aggregates: dict[str, Any]                  # revenue/cost/margin план/факт/Δ
    factors: list[dict[str, Any]]               # каждый — FactorResult.asdict
    products_breakdown: list[dict[str, Any]]    # per-product дельта выручки и маржи
    method_notes: list[str]                     # человекочитаемые заметки методологии
    parser_warnings: list[str] = field(default_factory=list)
    # Slice 11 typed adapter. Legacy ``factors`` stays for the existing XLSX
    # renderer, but both views are now produced from the same code-only math.
    decompositions: list[dict[str, Any]] = field(default_factory=list)
    # Сырые входы по cost-item — нужны для per-product формулы в xlsx-отчёте
    # ((c_F − c_P) × Q_F = … вместо повторяющейся агрегатной формулы).
    # Ключи: {item_id: {product_code: cost_per_unit_in_rubles}}.
    plan_unit_costs: dict[str, dict[str, float]] = field(default_factory=dict)
    fact_unit_costs: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────────────────
# Загрузка реестра факторов
# ──────────────────────────────────────────────────────────────────────

def load_factor_registry() -> dict[str, Any]:
    with open(_CONFIG_DIR / "factors.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────────────
# Helpers — преобразование parser-структур в плоские словари
# ──────────────────────────────────────────────────────────────────────

def _plan_fact_qty_price(period: PeriodTable) -> tuple[dict[str, float], dict[str, float]]:
    qty = {p["code"]: float(p["qty"]) for p in period.products}
    price = {p["code"]: float(p["price"]) for p in period.products}
    return qty, price


def _unit_cost_for_item(cost: CostTable, item_id: str) -> dict[str, float]:
    return {code: float(items.get(item_id, 0)) for code, items in cost.cost_per_unit.items()}


def _apply_product_filter(period: PeriodTable, allowed: set[str] | None) -> PeriodTable:
    if not allowed:
        return period
    return PeriodTable(
        label=period.label,
        products=[p for p in period.products if p["code"] in allowed],
    )


def _filter_cost(cost: CostTable, allowed: set[str] | None) -> CostTable:
    if not allowed:
        return cost
    return CostTable(
        label=cost.label,
        cost_per_unit={k: v for k, v in cost.cost_per_unit.items() if k in allowed},
    )


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def analyze(
    plan_path: Path | str,
    fact_path: Path | str,
    nomenclature_path: Path | str | None = None,
    scope: dict[str, Any] | None = None,
    period_label: str = "",
) -> Report:
    """Run end-to-end factor decomposition.

    Args:
        plan_path, fact_path: xlsx-файлы (нормированный формат, см. parser.py)
        nomenclature_path: справочник (optional — нужен только для подсветки
            "норматив гарантии" в комментариях, без него анализ продолжится)
        scope: {"products": [...], "factors": [...], "preset": "default"|...}.
            None → preset "default" из factors.yaml.
        period_label: человеческое название периода ("Q1 2026")

    Возвращает Report, готовый к JSON-сериализации.
    """
    warnings: list[str] = []
    registry = load_factor_registry()
    factor_defs = {f["id"]: f for f in registry["factors"]}
    presets = registry.get("presets") or {}

    # ── 1. Резолвим scope: factors / products / method
    scope = scope or {}
    preset_name = scope.get("preset") or "default"
    preset = presets.get(preset_name, presets.get("default") or {})
    selected_ids: list[str] = (
        scope.get("factors")
        or preset.get("factors")
        or list(factor_defs.keys())
    )
    allowed_products: set[str] | None = (
        set(scope["products"]) if scope.get("products") else None
    )

    # ── 2. Парсим xlsx
    plan_period, plan_cost = parse_period_workbook(plan_path, "ПЛАН")
    fact_period, fact_cost = parse_period_workbook(fact_path, "ФАКТ")
    nomenclature: Nomenclature | None = None
    if nomenclature_path:
        try:
            nomenclature = parse_nomenclature(nomenclature_path)
        except Exception as e:
            warnings.append(f"Справочник не удалось прочитать: {e}")

    plan_period = _apply_product_filter(plan_period, allowed_products)
    fact_period = _apply_product_filter(fact_period, allowed_products)
    plan_cost = _filter_cost(plan_cost, allowed_products)
    fact_cost = _filter_cost(fact_cost, allowed_products)

    # ── 3. Считаем агрегаты
    plan_qty, plan_price = _plan_fact_qty_price(plan_period)
    fact_qty, fact_price = _plan_fact_qty_price(fact_period)

    revenue_plan = sum(plan_qty[c] * plan_price[c] for c in plan_qty)
    revenue_fact = sum(fact_qty[c] * fact_price[c] for c in fact_qty)

    # Себестоимость считается из cost_per_unit × qty (это то, как
    # реально устроены файлы — costs дан на единицу).
    def total_cost(qty: dict[str, float], cost_per_unit: dict[str, dict[str, float]]) -> float:
        total = 0.0
        for code, q in qty.items():
            for item_id, c in cost_per_unit.get(code, {}).items():
                total += q * c
        return total

    cost_plan = total_cost(plan_qty, plan_cost.cost_per_unit)
    cost_fact = total_cost(fact_qty, fact_cost.cost_per_unit)

    aggregates = {
        "revenue": {
            "plan": revenue_plan, "fact": revenue_fact,
            "delta": revenue_fact - revenue_plan,
            "delta_pct": (revenue_fact - revenue_plan) / revenue_plan * 100 if revenue_plan else 0,
        },
        "cost": {
            "plan": cost_plan, "fact": cost_fact,
            "delta": cost_fact - cost_plan,
            "delta_pct": (cost_fact - cost_plan) / cost_plan * 100 if cost_plan else 0,
        },
        "margin": {
            "plan": revenue_plan - cost_plan,
            "fact": revenue_fact - cost_fact,
            "delta": (revenue_fact - cost_fact) - (revenue_plan - cost_plan),
        },
    }

    # ── 4. Считаем факторы
    factor_results: list[FactorResult] = []

    # 4a. Маржинальная декомпозиция (headline-вид — 3 фактора, ChatGPT-style).
    # Включается ВСЕГДА — это основа дефолтного ответа агента.
    def _unit_cost_total(cost: CostTable, code: str) -> float:
        items = cost.cost_per_unit.get(code, {})
        return float(sum(items.values()))

    plan_qpc = {
        c: (plan_qty[c], plan_price[c], _unit_cost_total(plan_cost, c))
        for c in plan_qty
    }
    fact_qpc = {
        c: (fact_qty[c], fact_price[c], _unit_cost_total(fact_cost, c))
        for c in fact_qty
    }
    for fr in margin_decomposition(plan_qpc, fact_qpc):
        factor_results.append(fr)

    # Revenue triplet — считаем всегда, если хоть один из 3 в selected_ids
    revenue_ids = {"volume", "structure", "price"}
    if revenue_ids & set(selected_ids):
        plan_qp = {c: (plan_qty[c], plan_price[c]) for c in plan_qty}
        fact_qp = {c: (fact_qty[c], fact_price[c]) for c in fact_qty}
        for fr in revenue_decomposition(plan_qp, fact_qp):
            if fr.id in selected_ids:
                factor_results.append(fr)

    # Cost items
    cost_item_map = {
        "materials":      ("MATERIALS",      "Материалы и комплектующие",      "laspeyres"),
        "labor":          ("LABOR",          "ФОТ производственного персонала", "laspeyres"),
        "overhead_plant": ("OVERHEAD_PLANT", "Накладные расходы хоз.службы",    "absolute"),
        "overhead_mgmt":  ("OVERHEAD_MGMT",  "Накладные расходы УК",            "absolute"),
        "quality":        ("QUALITY",        "Затраты на качество (ОТК)",       "absolute"),
        "warranty":       ("WARRANTY",       "Гарантийные обязательства",       "absolute"),
    }
    for fid, (item_code, item_name, method) in cost_item_map.items():
        if fid not in selected_ids:
            continue
        plan_uc = _unit_cost_for_item(plan_cost, item_code)
        fact_uc = _unit_cost_for_item(fact_cost, item_code)
        if method == "laspeyres":
            fr = cost_item_decomposition(
                item_id=fid, item_name=item_name,
                plan_qty=plan_qty, fact_qty=fact_qty,
                plan_unit_cost=plan_uc, fact_unit_cost=fact_uc,
                method="laspeyres",
            )
        else:
            fr = cost_item_absolute(
                item_id=fid, item_name=item_name,
                plan_qty=plan_qty, fact_qty=fact_qty,
                plan_unit_cost=plan_uc, fact_unit_cost=fact_uc,
            )
        # знак: для cost-факторов "рост" = негативный вклад в маржу.
        # Чтобы суммировать с revenue-факторами в маржу, переворачиваем знак.
        # Но в Report показываем positive=затраты выросли, чтобы не путать заказчика.
        factor_results.append(fr)

    # ── 5. Per-product breakdown — выручка и маржа
    products_breakdown: list[dict[str, Any]] = []
    plan_by_code = {p["code"]: p for p in plan_period.products}
    fact_by_code = {p["code"]: p for p in fact_period.products}
    for code in sorted(plan_by_code.keys() | fact_by_code.keys()):
        pp = plan_by_code.get(code) or {}
        ff = fact_by_code.get(code) or {}
        rev_p = (pp.get("qty") or 0) * (pp.get("price") or 0)
        rev_f = (ff.get("qty") or 0) * (ff.get("price") or 0)
        cost_p = sum(plan_cost.cost_per_unit.get(code, {}).values()) * (pp.get("qty") or 0)
        cost_f = sum(fact_cost.cost_per_unit.get(code, {}).values()) * (ff.get("qty") or 0)
        products_breakdown.append({
            "code": code,
            "name": (pp.get("name") or ff.get("name")),
            "qty_plan": pp.get("qty"),
            "qty_fact": ff.get("qty"),
            "price_plan": pp.get("price"),
            "price_fact": ff.get("price"),
            "revenue_plan": rev_p,
            "revenue_fact": rev_f,
            "revenue_delta": rev_f - rev_p,
            "cost_plan": cost_p,
            "cost_fact": cost_f,
            "margin_plan": rev_p - cost_p,
            "margin_fact": rev_f - cost_f,
            "margin_delta": (rev_f - cost_f) - (rev_p - cost_p),
        })

    # ── 6. Методологические заметки (для трейсабильности и презентации)
    method_notes = [
        f"Пресет: {preset_name} — {preset.get('explanation', '')}",
        "Декомпозиция выручки: цепная подстановка (Q → S → P), без остатка.",
        "Декомпозиция статей себестоимости: per-unit Δ × фактический объём "
        "(Laspeyres-вариант). Объёмная часть себестоимости остаётся в "
        "revenue-факторе 'Объём'.",
        "Знак cost-факторов: positive = затраты выросли (негативно для маржи).",
    ]
    if nomenclature:
        method_notes.append(
            f"Справочник: {len(nomenclature.products)} продукт(ов), "
            "плановые цены и нормативы гарантии загружены."
        )

    # Slice 11: migrate the existing tool consumer to typed decomposition
    # results. The workbook files are immutable inputs of this legacy entry
    # point, so content hashes are used as explicit provenance references.
    from finengine.skills.atoms.subject_history import adapt_factor_results, decompose_change

    def input_ref(path: Path | str) -> str:
        p = Path(path)
        return "input:sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

    input_refs = [input_ref(plan_path), input_ref(fact_path)]
    decompositions = []
    margin_factors = [f for f in factor_results if f.method == "margin_chain"]
    if margin_factors:
        decompositions.append(adapt_factor_results(
            concept="MARGIN", target_change=aggregates["margin"]["delta"],
            target_input_evidence_ids=input_refs, factors=margin_factors,
        ).model_dump(mode="json"))
    revenue_factors = [f for f in factor_results if f.method == "chain"]
    if revenue_factors:
        decompositions.append(adapt_factor_results(
            concept="REVENUE", target_change=aggregates["revenue"]["delta"],
            target_input_evidence_ids=input_refs, factors=revenue_factors,
        ).model_dump(mode="json"))
    cost_factors = [f for f in factor_results if f.group == "cost"]
    if cost_factors:
        decompositions.append(decompose_change(
            concept="COST", target_change=aggregates["cost"]["delta"],
            target_input_evidence_ids=input_refs,
            components=[{
                "component_id": f.id, "label": f.name, "value": f.value,
                "expression": f.formula_repr, "input_evidence_ids": input_refs,
            } for f in cost_factors],
            methodology_id="additive_v1", methodology_version="factor-analysis/1.0.0",
            tolerance=1e-6,
        ).model_dump(mode="json"))

    return Report(
        period_label=period_label or "",
        scope={
            "preset": preset_name,
            "factors": selected_ids,
            "products": sorted(allowed_products) if allowed_products else "ALL",
        },
        aggregates=aggregates,
        factors=[
            {
                "id": fr.id,
                "name": fr.name,
                "group": fr.group,
                "value": fr.value,
                "formula": fr.formula_repr,
                "method": fr.method,
                "by_product": fr.by_product,
            }
            for fr in factor_results
        ],
        products_breakdown=products_breakdown,
        method_notes=method_notes,
        parser_warnings=warnings,
        decompositions=decompositions,
        # raw inputs by cost item & product — для per-product формулы в xlsx.
        plan_unit_costs={
            fid: _unit_cost_for_item(plan_cost, cost_item_map[fid][0])
            for fid in cost_item_map
            if fid in selected_ids
        },
        fact_unit_costs={
            fid: _unit_cost_for_item(fact_cost, cost_item_map[fid][0])
            for fid in cost_item_map
            if fid in selected_ids
        },
    )
