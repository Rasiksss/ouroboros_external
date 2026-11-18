"""Factor Analysis skill — производственно-сбытовая декомпозиция отклонений.

Демо для ТМХ-Демидовский Q1 2026 (см. tests/factor_analysis_demo/).
Входит в состав FA-CLAW как реализация ТЗ ТМХ по ИИ-агенту факторного анализа.

API:

    analyze(plan_path, fact_path, nomenclature_path=None, scope=None) -> Report

`scope` принимает gibko-config:
    {
        "products": ["2ES6", ...] | None,           # None = все
        "factors": ["volume", "price", ...] | None, # None = все из factors.yaml
        "method": "chain" | "laspeyres" | "paasche",
    }

Архитектура — конфиг-first: добавление 10-го фактора = строчка в
config/factors.yaml, не Python-код. Это критично для "шаг влево, шаг
вправо" под заказчика.
"""
from .analyzer import analyze, Report  # noqa: F401
