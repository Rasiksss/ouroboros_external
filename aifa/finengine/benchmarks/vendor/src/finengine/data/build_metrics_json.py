"""Generate ``src/finengine/data/metrics.json`` as an aggregate of the baseline
per-cell registries (``src/finengine/data/registry/<profile>.json``).

The per-cell registries are the SOURCE OF TRUTH. metrics.json is a DERIVED
aggregate consumed by the non-cell paths — consensus, role-scope base, and the
cross-cutting classification in bulk_extractor (cost metrics, NF/bank split).
This replaces the retired Excel→metrics.json path (tests/build_metric_registry.py).

Aggregation:
  * NF cells (ifrs_nf_ru, ifrs_nf_en, us_gaap_nf_en)   → extractable/calculated.non_financial
    bank cells (ifrs_bank_ru, ifrs_bank_en, us_gaap_bank_en) → *.bank
  * union by enum; scalar fields (name_ru/name_en/description/hint/period_type/
    standard/formula_default_abs_behavior) taken from the first cell (in the
    fixed order below) that has the enum; document_types unioned across cells;
  * aliases_ru unioned across RU cells, aliases_en across EN cells;
  * ``role`` (extractable) and ``name_en``/``role``/``hint`` (calculated) are
    metadata absent from per-cell files — carried over from the existing
    metrics.json by enum ("" for genuinely new enums);
  * enum ORDER preserved from the existing metrics.json (new enums appended),
    so the diff stays readable.

Run:  PYTHONPATH=. .venv/bin/python src/finengine/data/build_metrics_json.py
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
REG = DATA / "registry"
OUT = DATA / "metrics.json"

# Fixed cell order per company_type (RU cell first = representative scalars).
CELLS = {
    "non_financial": ["ifrs_nf_ru", "ifrs_nf_en", "us_gaap_nf_en"],
    "bank": ["ifrs_bank_ru", "ifrs_bank_en", "us_gaap_bank_en"],
}
RU_CELLS = {"ifrs_nf_ru", "ifrs_bank_ru"}
DEFAULT_DOCUMENT_TYPES = ["financial_report"]


def _load(pid: str) -> dict:
    return json.loads((REG / f"{pid}.json").read_text(encoding="utf-8"))


def _old() -> dict:
    return json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {
        "extractable": {"non_financial": [], "bank": []},
        "calculated": {"non_financial": [], "bank": []},
    }


def _ordered(old_rows: list[dict], built: dict[str, dict]) -> list[dict]:
    """Emit built rows in the old file's enum order, then append new enums."""
    seen, out = set(), []
    for r in old_rows:
        e = r["enum"]
        if e in built:
            out.append(built[e])
            seen.add(e)
    for e, row in built.items():
        if e not in seen:
            out.append(row)
    return out


def build() -> dict:
    old = _old()
    warnings: list[str] = []
    result = {"extractable": {}, "calculated": {}}

    for ct, cells in CELLS.items():
        loaded = {c: _load(c) for c in cells}
        old_role = {r["enum"]: r.get("role", "") for r in old["extractable"][ct]}

        # --- extractable ---
        built: dict[str, dict] = {}
        for c in cells:
            for row in loaded[c]["extractable"]:
                e = row["enum"]
                if e not in built:
                    built[e] = {
                        "enum": e,
                        "name_ru": row.get("name_ru", ""),
                        "name_en": row.get("name_en", ""),
                        "aliases_ru": [],
                        "aliases_en": [],
                        "description": row.get("description", ""),
                        "hint": row.get("hint", ""),
                        "period_type": row.get("period_type", ""),
                        "standard": row.get("standard", ""),
                        "formula_default_abs_behavior": row.get("formula_default_abs_behavior"),
                        "document_types": [],
                        "role": old_role.get(e, ""),
                    }
                elif row.get("period_type") != built[e]["period_type"]:
                    warnings.append(
                        f"{ct}/{e}: period_type conflict "
                        f"({built[e]['period_type']!r} kept vs {row.get('period_type')!r} in {c})"
                    )
                # aliases: RU cells → aliases_ru, EN cells → aliases_en
                dst = "aliases_ru" if c in RU_CELLS else "aliases_en"
                src = row.get("aliases_ru" if c in RU_CELLS else "aliases_en") or []
                for a in src:
                    if a not in built[e][dst]:
                        built[e][dst].append(a)
                for document_type in row.get("document_types") or DEFAULT_DOCUMENT_TYPES:
                    if document_type not in built[e]["document_types"]:
                        built[e]["document_types"].append(document_type)
        # Keep the default implicit so report-only rows do not create a noisy
        # derived-file diff. Runtime loaders apply the same default.
        for row in built.values():
            if row["document_types"] == DEFAULT_DOCUMENT_TYPES:
                row.pop("document_types")
        result["extractable"][ct] = _ordered(old["extractable"][ct], built)

        # --- calculated (name_en/role/hint carried from old; formula/deps from per-cell) ---
        old_calc = {r["enum"]: r for r in old["calculated"][ct]}
        cbuilt: dict[str, dict] = {}
        for c in cells:
            for row in loaded[c]["calculated"]:
                e = row["enum"]
                if e in cbuilt:
                    continue
                o = old_calc.get(e, {})
                cbuilt[e] = {
                    "enum": e,
                    "name_ru": row.get("name_ru", ""),
                    "name_en": o.get("name_en", ""),
                    "formula": row.get("formula", ""),
                    "deps": row.get("deps", []),
                    "role": o.get("role", ""),
                    "hint": o.get("hint", ""),
                }
        result["calculated"][ct] = _ordered(old["calculated"][ct], cbuilt)

    # Alphabetical keys WITHIN each row (matches the old file's row shape) while
    # keeping the top-level section order (extractable→calculated, nf→bank).
    for sect in result:
        for ct in result[sect]:
            result[sect][ct] = [dict(sorted(r.items())) for r in result[sect][ct]]
    for w in warnings:
        print("WARN:", w)
    return result


if __name__ == "__main__":
    data = build()
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n = {
        f"{s}/{ct}": len(data[s][ct])
        for s in ("extractable", "calculated")
        for ct in ("non_financial", "bank")
    }
    print("wrote", OUT, n)
