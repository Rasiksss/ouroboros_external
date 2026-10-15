"""Convert the editable Excel metric registries into a JSON shipped with the backend.

Reads:
  tests/Реестр метрик_Extractables.xlsx   (sheet Лист1, header row 2)
  tests/Реестр метрик_Calculatables.xlsx (sheet Лист1, header row 2)

Writes:
  src/finengine/data/metrics.json
    {
      "extractable": {
        "non_financial": [{"enum","name_ru","name_en","aliases_ru","aliases_en","standard","role"}, ...],
        "bank": [...],
      },
      "calculated": {
        "non_financial": [{"enum","name_ru","name_en","formula","role"}, ...],
        "bank": [...],
      },
    }

Run:
  PYTHONPATH=. .venv/bin/python tests/build_metric_registry.py
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
EXTR = ROOT / "tests/Реестр метрик_Extractables.xlsx"
CALC = ROOT / "tests/Реестр метрик_Calculatables.xlsx"
OUT = ROOT / "src/finengine/data/metrics.json"


def nfc(s: object) -> str:
    if s is None:
        return ""
    return unicodedata.normalize("NFC", str(s)).strip()


def find_header(ws) -> tuple[int, list[str]]:
    for r in range(1, 6):
        cells = [nfc(c) for c in next(ws.iter_rows(min_row=r, max_row=r, values_only=True))]
        if any("MetricEnum" == c for c in cells):
            return r, cells
    raise RuntimeError(f"Header row not found in {ws.title}")


def col_index(header: list[str], name: str) -> int | None:
    for i, h in enumerate(header):
        if h.strip() == name:
            return i
    return None


def split_aliases(s: str) -> list[str]:
    if not s:
        return []
    parts = [p.strip() for p in s.replace(";", ",").replace("\n", ",").split(",")]
    return [p for p in parts if p and p != "Нет"]


def _load_enrichment() -> dict[str, dict]:
    """Load src/finengine/data/metric_enrichment.py without importing finengine."""
    import importlib.util
    src = ROOT / "src/finengine/data/metric_enrichment.py"
    spec = importlib.util.spec_from_file_location("metric_enrichment", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ENRICHMENT


def _merge_enrichment(item: dict, enrichment: dict[str, dict]) -> dict:
    """Excel data takes priority — only fill empties from the enrichment dict."""
    e = enrichment.get(item["enum"])
    if not e:
        item.setdefault("description", "")
        item.setdefault("hint", "")
        return item
    if not item.get("aliases_ru"):
        item["aliases_ru"] = list(e.get("aliases_ru", []))
    if not item.get("aliases_en"):
        item["aliases_en"] = list(e.get("aliases_en", []))
    item["description"] = item.get("description") or e.get("description", "")
    item["hint"] = item.get("hint") or e.get("hint", "")
    return item


def parse_extractables() -> dict[str, list[dict]]:
    wb = openpyxl.load_workbook(EXTR, data_only=True)
    ws = wb.active
    hdr_row, header = find_header(ws)

    i_enum = col_index(header, "MetricEnum")
    i_ru = col_index(header, "Название на русском")
    i_en = col_index(header, "Название на английском")
    i_syn_ru = col_index(header, "Синонимы на русском")
    i_syn_en = col_index(header, "Синонимы на английском")
    i_std = col_index(header, "Стандарт отчетности")
    i_role = col_index(header, "Роль")
    i_type = col_index(header, "Тип компании")
    # Optional descriptions/hints if user added these columns to Excel.
    i_desc = col_index(header, "Описание") or col_index(header, "Description")
    i_hint = col_index(header, "Где искать") or col_index(header, "Подсказка") or col_index(header, "Hint")

    enrichment = _load_enrichment()

    nf: list[dict] = []
    bank: list[dict] = []
    for row in ws.iter_rows(min_row=hdr_row + 1, values_only=True):
        enum = nfc(row[i_enum])
        if not enum:
            continue
        typ = nfc(row[i_type])
        item = {
            "enum": enum,
            "name_ru": nfc(row[i_ru]),
            "name_en": nfc(row[i_en]) if i_en is not None else "",
            "aliases_ru": split_aliases(nfc(row[i_syn_ru]) if i_syn_ru is not None else ""),
            "aliases_en": split_aliases(nfc(row[i_syn_en]) if i_syn_en is not None else ""),
            "description": nfc(row[i_desc]) if i_desc is not None else "",
            "hint": nfc(row[i_hint]) if i_hint is not None else "",
            "standard": nfc(row[i_std]) if i_std is not None else "",
            "role": nfc(row[i_role]) if i_role is not None else "",
        }
        item = _merge_enrichment(item, enrichment)
        if "Нефинансовые" in typ:
            nf.append(item)
        if "Кредитные" in typ:
            bank.append(item)
    return {"non_financial": nf, "bank": bank}


_DEP_RE = __import__("re").compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def parse_formula(raw: str, self_enum: str) -> tuple[str, list[str]]:
    """Strip the "ENUM = " prefix; pull dependency enums from the RHS."""
    s = raw.strip()
    if "=" in s:
        s = s.split("=", 1)[1].strip()
    deps = []
    for m in _DEP_RE.finditer(s):
        token = m.group(0)
        if token == self_enum:
            continue
        if token in deps:
            continue
        deps.append(token)
    return s, deps


def parse_calculated() -> dict[str, list[dict]]:
    wb = openpyxl.load_workbook(CALC, data_only=True)
    ws = wb.active
    hdr_row, header = find_header(ws)

    i_enum = col_index(header, "MetricEnum")
    i_ru = col_index(header, "Название на русском")
    i_en = col_index(header, "Название на английском")
    i_formula = col_index(header, "Формула (Через MetricEnum)")
    i_role = col_index(header, "Роль")
    i_type = col_index(header, "Тип компании")

    nf: list[dict] = []
    bank: list[dict] = []
    for row in ws.iter_rows(min_row=hdr_row + 1, values_only=True):
        enum = nfc(row[i_enum])
        if not enum:
            continue
        typ = nfc(row[i_type])
        formula_raw = nfc(row[i_formula]) if i_formula is not None else ""
        rhs, deps = parse_formula(formula_raw, enum)
        item = {
            "enum": enum,
            "name_ru": nfc(row[i_ru]),
            "name_en": nfc(row[i_en]) if i_en is not None else "",
            "formula": rhs,
            "deps": deps,
            "role": nfc(row[i_role]) if i_role is not None else "",
        }
        if "Нефинансовые" in typ:
            nf.append(item)
        if "Кредитные" in typ:
            bank.append(item)
    return {"non_financial": nf, "bank": bank}


def main() -> None:
    extractable = parse_extractables()
    calculated = parse_calculated()
    data = {"extractable": extractable, "calculated": calculated}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved → {OUT}")
    print(
        f"  Extractable:  NF {len(extractable['non_financial'])}  /  Bank {len(extractable['bank'])}"
    )
    print(
        f"  Calculated:   NF {len(calculated['non_financial'])}  /  Bank {len(calculated['bank'])}"
    )


if __name__ == "__main__":
    main()
