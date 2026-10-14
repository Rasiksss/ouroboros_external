"""User-uploaded metric registry — structural validation + overlay write.

Phase 2 of registry unification. A role owner uploads a filled template that
extends their role's metrics; each new/updated metric is written (role-tagged)
into the per-cell overlays (``data/registry_overlays/<cell>.json``) for the
cells the user marked. The matrix picks them up at runtime via
``metric_schema.registry_for_profile`` (v1: overlays extend the per-cell matrix
path only — the base ``metrics.json`` is not regenerated here).

A row (one metric) coming from the parsed template:
    {enum, kind: "extractable"|"calculated", name_ru, name_en, aliases_ru,
     aliases_en, description, hint, period_type, value_basis, formula, deps,
     cells: [profile_id, ...]}
``role`` is applied server-side from the uploader — never trusted from the row.
"""
from __future__ import annotations

import io
import json
import re

from finengine.skills.metric_schema import (
    PROFILE_IDS,
    VALID_PERIOD_TYPES,
    _overlay_dir,
)

_ENUM_RE = re.compile(r"[A-Z][A-Z0-9_]*")

# xlsx template: "Показатели" sheet columns (machine keys in the header row;
# the "Инструкция" sheet explains them in Russian). The 6 profile-id columns
# follow; an X mark selects the cells the metric applies to.
TEMPLATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("enum", "Код метрики: заглавные латинские буквы, цифры, _ (например MY_KPI)"),
    ("kind", "extractable (извлекаемый из отчёта) или calculated (рассчитываемый по формуле)"),
    ("name_ru", "Название на русском"),
    ("name_en", "Название на английском"),
    ("description", "Что это за показатель (для модели)"),
    ("hint", "Где искать в отчёте / как не перепутать (для модели)"),
    ("period_type", "point (на дату) или duration (за период); для calculated не нужно"),
    ("formula", "Только для calculated: формула из кодов метрик, например MY_KPI / TOTAL_ASSETS * 100%"),
    ("deps", "Только для calculated: коды метрик из формулы через ;"),
    ("aliases_ru", "Синонимы строк отчёта на русском, через ;"),
    ("aliases_en", "Синонимы строк отчёта на английском, через ;"),
    ("value_basis", "gross / net — если важно (иначе пусто)"),
)

_HEADER_ALIASES: dict[str, str] = {
    "код": "enum", "код метрики": "enum", "код (enum)": "enum",
    "тип": "kind", "вид": "kind",
    "название ru": "name_ru", "название рус": "name_ru", "название на русском": "name_ru",
    "название en": "name_en", "название англ": "name_en", "название на английском": "name_en",
    "описание": "description",
    "подсказка": "hint", "подсказка (где искать)": "hint", "где искать": "hint",
    "тип периода": "period_type",
    "формула": "formula",
    "зависимости": "deps",
    "алиасы ru": "aliases_ru", "синонимы ru": "aliases_ru",
    "алиасы en": "aliases_en", "синонимы en": "aliases_en",
    "база значения": "value_basis",
}

_KIND_ALIASES = {
    "extractable": "extractable", "извлекаемый": "extractable", "извлекаемая": "extractable",
    "calculated": "calculated", "рассчитываемый": "calculated", "расчетный": "calculated",
    "расчётный": "calculated", "рассчитываемая": "calculated",
}

_PERIOD_TYPE_ALIASES = {
    "point": "point", "на дату": "point",
    "duration": "duration", "за период": "duration",
    "": "",
}

# латинская x/X и русская х/Х, плюс обычные отметки
_CELL_MARKS = {"x", "х", "+", "1", "да", "yes", "true", "v"}


def _has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[а-яА-ЯёЁ]", s or ""))


def _has_latin(s: str) -> bool:
    return bool(re.search(r"[a-zA-Z]", s or ""))


def validate_row(row: dict) -> list[str]:
    """Structural validation only — catches obviously-broken rows (wrong types,
    swapped RU/EN columns, junk), NOT hint/description *quality*. Returns a list
    of human-readable error strings; empty = row is acceptable."""
    errs: list[str] = []
    enum = (row.get("enum") or "").strip()
    if not enum:
        errs.append("пустой enum")
    elif not _ENUM_RE.fullmatch(enum):
        errs.append(f"enum «{enum}» — только заглавные латинские, цифры и _")

    kind = (row.get("kind") or "extractable").strip().lower()
    if kind not in ("extractable", "calculated"):
        errs.append(f"kind «{kind}» — должно быть extractable или calculated")

    name_ru = (row.get("name_ru") or "").strip()
    name_en = (row.get("name_en") or "").strip()
    if not name_ru and not name_en:
        errs.append("нет ни названия RU, ни EN")
    if name_ru and _has_latin(name_ru) and not _has_cyrillic(name_ru):
        errs.append("название RU выглядит латиницей — перепутаны колонки?")
    if name_en and _has_cyrillic(name_en):
        errs.append("название EN содержит кириллицу — перепутаны колонки?")

    desc = (row.get("description") or "").strip()
    if desc and desc.isdigit():
        errs.append("описание — одни цифры")

    if kind == "extractable":
        pt = (row.get("period_type") or "").strip()
        if pt not in VALID_PERIOD_TYPES:
            errs.append(f"period_type «{pt}» — point, duration или пусто")
    if kind == "calculated" and not (row.get("formula") or "").strip():
        errs.append("рассчитываемый показатель без формулы")

    cells = row.get("cells") or []
    if not cells:
        errs.append("не отмечена ни одна ячейка")
    unknown = [c for c in cells if c not in PROFILE_IDS]
    if unknown:
        errs.append(f"неизвестные ячейки: {', '.join(unknown)}")
    return errs


def _extractable_payload(row: dict, role: str) -> dict:
    return {
        "enum": (row.get("enum") or "").strip(),
        "name_ru": (row.get("name_ru") or "").strip(),
        "name_en": (row.get("name_en") or "").strip(),
        "aliases_ru": list(row.get("aliases_ru") or []),
        "aliases_en": list(row.get("aliases_en") or []),
        "description": (row.get("description") or "").strip(),
        "hint": (row.get("hint") or "").strip(),
        "period_type": (row.get("period_type") or "").strip(),
        "value_basis": row.get("value_basis") or None,
        "standard": (row.get("standard") or "").strip(),
        "role": role,
        "source": "user_upload",
    }


def _calculated_payload(row: dict, role: str) -> dict:
    return {
        "enum": (row.get("enum") or "").strip(),
        "name_ru": (row.get("name_ru") or "").strip(),
        "formula": (row.get("formula") or "").strip(),
        "deps": list(row.get("deps") or []),
        "role": role,
        "source": "user_upload",
    }


def write_overlay(
    profile_id: str, extractable: list[dict], calculated: list[dict]
) -> tuple[int, int]:
    """Merge rows into ``data/registry_overlays/<profile_id>.json`` (create the
    file/dir if needed). Rows override existing overlay rows by enum and append
    new ones. Returns ``(added, updated)`` counts."""
    d = _overlay_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile_id}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"extractable": [], "calculated": []}

    added = updated = 0
    for key, rows in (("extractable", extractable), ("calculated", calculated)):
        existing = data.setdefault(key, [])
        pos = {r["enum"]: i for i, r in enumerate(existing)}
        for row in rows:
            e = row["enum"]
            if e in pos:
                existing[pos[e]] = row
                updated += 1
            else:
                existing.append(row)
                pos[e] = len(existing) - 1
                added += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added, updated


def apply_upload(role: str, rows: list[dict]) -> dict:
    """Validate every row, then write the valid ones into the marked cells'
    overlays with the uploader's ``role`` tag. Returns a report the UI shows."""
    role = (role or "").strip().lower()
    valid: list[dict] = []
    rejected: list[dict] = []
    for i, row in enumerate(rows):
        errs = validate_row(row)
        if errs:
            rejected.append({"row": i + 1, "enum": (row.get("enum") or "").strip(), "errors": errs})
        else:
            valid.append(row)

    by_cell: dict[str, dict[str, list]] = {}
    for row in valid:
        kind = (row.get("kind") or "extractable").strip().lower()
        for cell in row["cells"]:
            slot = by_cell.setdefault(cell, {"extractable": [], "calculated": []})
            if kind == "calculated":
                slot["calculated"].append(_calculated_payload(row, role))
            else:
                slot["extractable"].append(_extractable_payload(row, role))

    added = updated = 0
    for cell, slots in by_cell.items():
        a, u = write_overlay(cell, slots["extractable"], slots["calculated"])
        added += a
        updated += u

    return {
        "role": role,
        "added": added,
        "updated": updated,
        "rejected": rejected,
        "accepted_rows": len(valid),
        "cells": sorted(by_cell),
    }


def _norm_header(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _split_list(value: object, *, allow_comma: bool) -> list[str]:
    pattern = r"[;,\n]" if allow_comma else r"[;\n]"
    return [p.strip() for p in re.split(pattern, str(value or "")) if p.strip()]


def build_template_workbook(role: str) -> io.BytesIO:
    """xlsx template the role owner downloads: an empty "Показатели" sheet to
    fill in, an "Инструкция" sheet, and the role's CURRENT registry as a
    read-only reference sheet (per user's ask: download existing, add your own)."""
    import openpyxl
    from openpyxl.styles import Font

    from finengine.roles.scope import build_role_plan
    from finengine.skills.metric_schema import (
        _company_type_for_profile,
        registry_for_profile,
    )

    wb = openpyxl.Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Показатели"
    headers = [f for f, _ in TEMPLATE_FIELDS] + list(PROFILE_IDS)
    ws.append(headers)
    for cell in ws[1]:
        cell.font = bold
    widths = [24, 12, 28, 28, 40, 40, 12, 30, 22, 32, 32, 12] + [16] * len(PROFILE_IDS)
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width

    ref = wb.create_sheet("Текущий реестр (справочно)")
    ref.append(["enum", "kind", "name_ru", "name_en", "period_type", "ячейки"])
    for cell in ref[1]:
        cell.font = bold
    info: dict[str, dict] = {}
    for profile_id in PROFILE_IDS:
        reg = registry_for_profile(profile_id)
        plan = build_role_plan(role, _company_type_for_profile(profile_id), profile_id=profile_id)
        allowed = plan.output_enums  # None => generic role, whole cell registry
        for m in reg["extractable"]:
            if allowed is not None and m["enum"] not in allowed:
                continue
            slot = info.setdefault(m["enum"], {
                "kind": "extractable", "name_ru": m["name_ru"],
                "name_en": m.get("name_en", ""), "period_type": m.get("period_type", ""),
                "cells": set(),
            })
            slot["cells"].add(profile_id)
        for enum, name_ru, _formula, _deps in reg["calculated"]:
            if allowed is not None and enum not in allowed:
                continue
            slot = info.setdefault(enum, {
                "kind": "calculated", "name_ru": name_ru,
                "name_en": "", "period_type": "", "cells": set(),
            })
            slot["cells"].add(profile_id)
    for enum in sorted(info):
        slot = info[enum]
        ref.append([
            enum, slot["kind"], slot["name_ru"], slot["name_en"],
            slot["period_type"], ", ".join(sorted(slot["cells"])),
        ])
    for idx, width in enumerate([28, 12, 40, 40, 12, 60], start=1):
        ref.column_dimensions[ref.cell(row=1, column=idx).column_letter].width = width

    inst = wb.create_sheet("Инструкция")
    inst.append(["Поле", "Как заполнять"])
    for cell in inst[1]:
        cell.font = bold
    for field, howto in TEMPLATE_FIELDS:
        inst.append([field, howto])
    inst.append([])
    inst.append(["ячейки", "Поставьте X в колонках ячеек (ifrs_nf_ru и т.д.), к которым относится показатель. Хотя бы одна обязательна."])
    inst.append(["роль", "Не указывается в файле: показатели привяжутся к роли, от имени которой вы загружаете."])
    inst.append(["повторная загрузка", "Строка с существующим кодом обновляет показатель, а не дублирует его."])
    inst.append(["пример", "MY_SHARE | calculated | Доля моего сегмента | My segment share | ... | формула: SALES_REVENUE_SEGMENT / SALES_REVENUE * 100% | deps: SALES_REVENUE"])
    inst.column_dimensions["A"].width = 22
    inst.column_dimensions["B"].width = 110

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def parse_upload_workbook(content: bytes) -> tuple[list[dict], list[str]]:
    """Parse a filled template back into ``apply_upload`` rows. Returns
    ``(rows, file_errors)``; file_errors are workbook-level problems (bad file,
    missing header) — per-row problems are validate_row's job."""
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception:
        return [], ["файл не читается как xlsx"]

    ws = wb["Показатели"] if "Показатели" in wb.sheetnames else wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header: list[str] = []
    for raw in rows_iter:
        if any(str(v or "").strip() for v in raw):
            header = [_norm_header(v) for v in raw]
            break
    if not header:
        return [], ["в листе «Показатели» нет строки заголовков"]

    col_field: dict[int, str] = {}
    profile_by_norm = {_norm_header(p): p for p in PROFILE_IDS}
    # _norm_header turns underscores into spaces, so normalize the machine
    # keys the same way ("period_type" header -> "period type")
    known_by_norm = {_norm_header(f): f for f, _ in TEMPLATE_FIELDS}
    for idx, name in enumerate(header):
        if name in known_by_norm:
            col_field[idx] = known_by_norm[name]
        elif name in _HEADER_ALIASES:
            col_field[idx] = _HEADER_ALIASES[name]
        elif name in profile_by_norm:
            col_field[idx] = f"cell:{profile_by_norm[name]}"
    if "enum" not in col_field.values():
        return [], ["не найдена колонка enum (код метрики)"]

    rows: list[dict] = []
    for raw in rows_iter:
        if not any(str(v or "").strip() for v in raw):
            continue
        row: dict = {"cells": []}
        for idx, field in col_field.items():
            value = raw[idx] if idx < len(raw) else None
            text = str(value).strip() if value is not None else ""
            if field.startswith("cell:"):
                if text.lower() in _CELL_MARKS:
                    row["cells"].append(field.removeprefix("cell:"))
            elif field in ("aliases_ru", "aliases_en"):
                row[field] = _split_list(text, allow_comma=False)
            elif field == "deps":
                row[field] = _split_list(text, allow_comma=True)
            elif field == "kind":
                row[field] = _KIND_ALIASES.get(text.lower(), text.lower())
            elif field == "period_type":
                row[field] = _PERIOD_TYPE_ALIASES.get(text.lower(), text)
            else:
                row[field] = text
        rows.append(row)
    return rows, []
