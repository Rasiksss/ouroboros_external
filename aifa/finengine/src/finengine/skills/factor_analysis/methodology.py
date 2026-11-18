"""Factor decomposition math — Laspeyres, цепная подстановка, абсолютные дельты.

Здесь живут только формулы; парсинг (parser.py) и оркестрация (analyzer.py)
отдельно. Это позволяет покрыть методологию юнит-тестами без xlsx-фикстур.

Терминология:
    P — план, F — факт.
    Q (qty), p (price), s (structure share), c (cost per unit).

Полное отклонение выручки:
    R_F − R_P = Σ_i (Q_F[i]·p_F[i]) − Σ_i (Q_P[i]·p_P[i])

В цепной подстановке (порядок: объём → структура → цена) промежуточные
переменные:

    R0  = Σ Q_P · p_P                                       (исходный план)
    R1  = Q_F_total · Σ_i s_P[i] · p_P[i]                   (объём → факт)
    R2  = Σ Q_F · p_P                                       (+структура)
    R3  = Σ Q_F · p_F = R_F                                 (+цена)

    factor_volume    = R1 − R0
    factor_structure = R2 − R1
    factor_price     = R3 − R2

Σ факторов = R_F − R_P (без остатка), что является ключевым свойством
chain-substitution для презентации заказчику.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FactorResult:
    """Один фактор по одной (или агрегированной) метрике."""
    id: str
    name: str
    group: str
    value: float                # ₽ (вклад в отклонение)
    formula_repr: str           # как считалось — для трейсабильности
    method: str
    by_product: dict[str, float] | None = None  # разбивка по продуктам


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


# ──────────────────────────────────────────────────────────────────────
# Revenue factors: volume + structure + price (chain substitution)
# ──────────────────────────────────────────────────────────────────────

def revenue_decomposition(
    plan: dict[str, tuple[float, float]],
    fact: dict[str, tuple[float, float]],
) -> list[FactorResult]:
    """
    plan/fact: {product_code: (qty, price)}.
    Returns 3 factors: volume, structure, price.

    Алгоритм — цепная подстановка в порядке Q → S → P:
      R0 = Σ q_p · p_p
      R1 = q_F_total · Σ (s_P · p_P)
      R2 = Σ q_F · p_P
      R3 = Σ q_F · p_F
    """
    codes = list(plan.keys() | fact.keys())
    qP = {c: plan.get(c, (0, 0))[0] for c in codes}
    pP = {c: plan.get(c, (0, 0))[1] for c in codes}
    qF = {c: fact.get(c, (0, 0))[0] for c in codes}
    pF = {c: fact.get(c, (0, 0))[1] for c in codes}

    QP_total = sum(qP.values())
    QF_total = sum(qF.values())
    sP = {c: _safe_div(qP[c], QP_total) for c in codes}

    # R0 = план
    R0 = sum(qP[c] * pP[c] for c in codes)
    # R1 = объём по факту, цены и структура по плану
    R1 = QF_total * sum(sP[c] * pP[c] for c in codes)
    # R2 = объём и структура по факту, цены план
    R2 = sum(qF[c] * pP[c] for c in codes)
    # R3 = всё по факту
    R3 = sum(qF[c] * pF[c] for c in codes)

    # Разбивки по продуктам — для каждого фактора удобно показать "кто внёс"
    by_product_volume = {c: (qF[c] - qP[c]) * pP[c] * sP.get(c, 0) * 0 for c in codes}
    # ↑ упрощение: на уровне продукта объёмный фактор = ΔQ × P_план,
    # без декомпозиции через QF_total — иначе невозможно интерпретировать
    # вклад одного продукта. Это compatible представление, агрегат
    # может расходиться с R1−R0 на величину structure-эффекта.
    by_product_volume = {c: (qF[c] - qP[c]) * pP[c] for c in codes}
    by_product_structure_proxy: dict[str, float] = {}
    # structure по продукту — остаток после "чистого объёма" и "цены" на уровне продукта.
    # См. methodology.md, раздел "Аддитивность".
    by_product_price = {c: qF[c] * (pF[c] - pP[c]) for c in codes}
    by_product_structure_proxy = {
        c: (qF[c] * pP[c]) - (qP[c] * pP[c]) - by_product_volume[c]
        for c in codes
    }

    return [
        FactorResult(
            id="volume",
            name="Объём реализации",
            group="revenue",
            value=R1 - R0,
            formula_repr=f"R1 − R0 = {R1:,.0f} − {R0:,.0f}",
            method="chain",
            by_product=by_product_volume,
        ),
        FactorResult(
            id="structure",
            name="Структура реализации",
            group="revenue",
            value=R2 - R1,
            formula_repr=f"R2 − R1 = {R2:,.0f} − {R1:,.0f}",
            method="chain",
            by_product=by_product_structure_proxy,
        ),
        FactorResult(
            id="price",
            name="Цена реализации",
            group="revenue",
            value=R3 - R2,
            formula_repr=f"R3 − R2 = {R3:,.0f} − {R2:,.0f}",
            method="chain",
            by_product=by_product_price,
        ),
    ]


# ──────────────────────────────────────────────────────────────────────
# Cost factors — каждая статья отдельно, по-продуктам
# ──────────────────────────────────────────────────────────────────────

def cost_item_decomposition(
    item_id: str,
    item_name: str,
    plan_qty: dict[str, float],
    fact_qty: dict[str, float],
    plan_unit_cost: dict[str, float],
    fact_unit_cost: dict[str, float],
    method: str = "laspeyres",
) -> FactorResult:
    """
    Декомпозиция одной статьи себестоимости — ПОЛНАЯ дельта.

    Plan: C_P = Σ Q_P · c_P
    Fact: C_F = Σ Q_F · c_F

    Laspeyres двухуровнево (показано в formula_repr для трейсабильности,
    но возвращается одно агрегатное значение Δ_статьи):
        Δнорма  = Σ (c_F − c_P) · Q_F           (удорожание удельных затрат)
        Δобъём  = Σ c_P · (Q_F − Q_P)           (рост из-за объёма по план.ставкам)
        Σ = C_F − C_P (точно, без остатка)

    Важно для аддитивности: Σ_всех_cost-факторов = Δcost_общая. Тогда
    Δmargin = Σ revenue-факторов − Σ cost-факторов, что и показываем
    заказчику.
    """
    codes = list(plan_qty.keys() | fact_qty.keys())
    delta_norm = sum(
        (fact_unit_cost.get(c, 0) - plan_unit_cost.get(c, 0)) * fact_qty.get(c, 0)
        for c in codes
    )
    delta_vol = sum(
        plan_unit_cost.get(c, 0) * (fact_qty.get(c, 0) - plan_qty.get(c, 0))
        for c in codes
    )
    total = delta_norm + delta_vol
    by_product = {
        c: (fact_unit_cost.get(c, 0) * fact_qty.get(c, 0))
           - (plan_unit_cost.get(c, 0) * plan_qty.get(c, 0))
        for c in codes
    }
    return FactorResult(
        id=item_id,
        name=item_name,
        group="cost",
        value=total,
        formula_repr=(
            f"Δнорма + Δобъём = {delta_norm:,.0f} + {delta_vol:,.0f} = {total:,.0f} ₽"
        ),
        method=method,
        by_product=by_product,
    )


def margin_decomposition(
    plan: dict[str, tuple[float, float, float]],
    fact: dict[str, tuple[float, float, float]],
) -> list[FactorResult]:
    """Декомпозиция маржи на 3 фактора (вариант, который ChatGPT/Code Interpreter
    обычно отдаёт по-умолчанию для разовой презентации).

    plan/fact: {product_code: (qty, price, unit_cost)}.
    Plan margin    = Σ Q₀ × (P₀ − C₀)
    Fact margin    = Σ Q₁ × (P₁ − C₁)
    Δ margin = volume_struct + price + unit_cost (точно, без остатка):
        volume_struct = Σ (Q₁ − Q₀) × (P₀ − C₀)
        price         = Σ Q₁ × (P₁ − P₀)
        unit_cost     = −Σ Q₁ × (C₁ − C₀)

    Используется параллельно с нашей 9-факторной декомпозицией, чтобы дать
    юзеру и сжатый headline-вид, и аналитическую детализацию.
    """
    codes = list(plan.keys() | fact.keys())
    qP = {c: plan.get(c, (0, 0, 0))[0] for c in codes}
    pP = {c: plan.get(c, (0, 0, 0))[1] for c in codes}
    cP = {c: plan.get(c, (0, 0, 0))[2] for c in codes}
    qF = {c: fact.get(c, (0, 0, 0))[0] for c in codes}
    pF = {c: fact.get(c, (0, 0, 0))[1] for c in codes}
    cF = {c: fact.get(c, (0, 0, 0))[2] for c in codes}

    vs = {c: (qF[c] - qP[c]) * (pP[c] - cP[c]) for c in codes}
    pr = {c: qF[c] * (pF[c] - pP[c]) for c in codes}
    uc = {c: -qF[c] * (cF[c] - cP[c]) for c in codes}

    return [
        FactorResult(
            id="margin_volume_struct",
            name="Объём / структура (маржа)",
            group="margin",
            value=sum(vs.values()),
            formula_repr="Σ (Q₁−Q₀) × (P₀−C₀)",
            method="margin_chain",
            by_product=vs,
        ),
        FactorResult(
            id="margin_price",
            name="Цена реализации (маржа)",
            group="margin",
            value=sum(pr.values()),
            formula_repr="Σ Q₁ × (P₁−P₀)",
            method="margin_chain",
            by_product=pr,
        ),
        FactorResult(
            id="margin_unit_cost",
            name="Себестоимость на единицу",
            group="margin",
            value=sum(uc.values()),
            formula_repr="−Σ Q₁ × (C₁−C₀)",
            method="margin_chain",
            by_product=uc,
        ),
    ]


def cost_item_absolute(
    item_id: str,
    item_name: str,
    plan_unit_cost: dict[str, float],
    fact_unit_cost: dict[str, float],
    plan_qty: dict[str, float],
    fact_qty: dict[str, float],
) -> FactorResult:
    """Абсолютная дельта по статье без декомпозиции на норматив/цену.
    Используется для накладных, где нет осмысленного "нормативa расхода".
    """
    codes = list(plan_qty.keys() | fact_qty.keys())
    plan_total = sum(plan_unit_cost.get(c, 0) * plan_qty.get(c, 0) for c in codes)
    fact_total = sum(fact_unit_cost.get(c, 0) * fact_qty.get(c, 0) for c in codes)
    by_product = {
        c: (fact_unit_cost.get(c, 0) * fact_qty.get(c, 0))
           - (plan_unit_cost.get(c, 0) * plan_qty.get(c, 0))
        for c in codes
    }
    return FactorResult(
        id=item_id,
        name=item_name,
        group="cost",
        value=fact_total - plan_total,
        formula_repr=f"C_F − C_P = {fact_total:,.0f} − {plan_total:,.0f}",
        method="absolute",
        by_product=by_product,
    )
