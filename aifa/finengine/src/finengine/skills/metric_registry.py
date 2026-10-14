"""Runtime metric registry — loaded from src/finengine/data/metrics.json.

The JSON is generated from `tests/Реестр метрик_Extractables.xlsx` and
`tests/Реестр метрик_Calculatables.xlsx` by `tests/build_metric_registry.py`.
Edit the Excel files, run the script, redeploy.

Counts as of 2026-04-27:
- Extractable: 77 non-financial / 77 bank (35 shared metrics, 42 segment-specific each)
- Calculated:  46 non-financial / 28 bank (6 shared, 40 NF-specific, 22 bank-specific)
"""
from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "metrics.json"

with _DATA_PATH.open("r", encoding="utf-8") as _f:
    _RAW = json.load(_f)


def _extractable_list(items: list[dict]) -> list[dict]:
    """Shape items for runtime: dict with enum, name_ru, name_en, aliases_ru,
    aliases_en, description, hint, period_type, formula_default_abs_behavior,
    standard, role."""
    out: list[dict] = []
    for it in items:
        out.append(
            {
                "enum": it["enum"],
                "name_ru": it["name_ru"],
                "name_en": it.get("name_en", ""),
                "aliases_ru": it.get("aliases_ru") or [],
                "aliases_en": it.get("aliases_en") or [],
                "description": it.get("description", ""),
                "hint": it.get("hint", ""),
                "standard": it.get("standard", ""),
                "role": it.get("role", ""),
                "value_basis": it.get("value_basis"),
                "period_type": it.get("period_type", ""),
                "formula_default_abs_behavior": bool(it.get("formula_default_abs_behavior", False)),
            }
        )
    return out


def _calculated_list(items: list[dict]) -> list[tuple[str, str, str, list[str]]]:
    """Existing call-sites expect tuples (enum, name_ru, formula, deps)."""
    return [(it["enum"], it["name_ru"], it.get("formula", ""), it.get("deps", []) or []) for it in items]


EXTRACTABLE_NON_FINANCIAL = _extractable_list(_RAW["extractable"]["non_financial"])
EXTRACTABLE_BANK = _extractable_list(_RAW["extractable"]["bank"])
CALCULATED_NON_FINANCIAL = _calculated_list(_RAW["calculated"]["non_financial"])
CALCULATED_BANK = _calculated_list(_RAW["calculated"]["bank"])

# Quick lookup dict by enum → metric meta. Useful for prompt building.
NF_BY_ENUM = {m["enum"]: m for m in EXTRACTABLE_NON_FINANCIAL}
BANK_BY_ENUM = {m["enum"]: m for m in EXTRACTABLE_BANK}
METRIC_PERIOD_TYPE_BY_ENUM = {
    m["enum"]: m.get("period_type", "")
    for m in [*EXTRACTABLE_NON_FINANCIAL, *EXTRACTABLE_BANK]
}
