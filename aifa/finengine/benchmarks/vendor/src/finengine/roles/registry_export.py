"""Consolidated registry export.

Flattens all per-cell registries into ONE read-only xlsx so an admin can see,
at a glance, what's on the server: for which standard / language / industry /
role each metric exists, plus its formula, hint, aliases. This is a generated
VIEW over the per-cell files (the source of truth) — not a second source.

Columns: Ячейка · Стандарт · Язык · Отрасль · Тип · Enum · Название RU/EN ·
Формула · Базис · Период · Модуль в формулах · Хинт · Описание · Алиасы ·
Спец. роли.

"Спец. роли" lists the specialised roles (CIB / ДГР / …) whose layout includes
the metric; blank = available only in the general role. Source: layouts.json
via build_role_plan.
"""
from __future__ import annotations

import io

from finengine.roles.access import EXTRACTION_ROLES, OTHER_ROLE, ROLE_LABELS
from finengine.roles.scope import build_role_plan
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

_HEADERS = [
    "Ячейка", "Стандарт", "Язык", "Отрасль", "Тип", "Enum",
    "Название RU", "Название EN", "Формула", "Базис", "Период",
    "Модуль в формулах", "Хинт", "Описание", "Алиасы", "Спец. роли",
]


def _axes(profile_id: str) -> tuple[str, str, str]:
    """(standard, company_type, language) from the cell id."""
    if profile_id.startswith("us_gaap_"):
        standard = "US GAAP"
    elif profile_id.startswith("ind_as_"):
        standard = "Ind AS"
    elif profile_id.startswith("ind_gaap_"):
        standard = "Indian GAAP"
    else:
        standard = "IFRS"
    company_type = "bank" if "_bank_" in profile_id else "non_financial"
    language = "ru" if profile_id.endswith("_ru") else "en"
    return standard, company_type, language


def _specialised_roles_by_ct_enum() -> dict[tuple[str, str], set[str]]:
    """{(company_type, ENUM): {role labels}} for specialised roles only — the
    general role covers everything and isn't informative per metric."""
    out: dict[tuple[str, str], set[str]] = {}
    for company_type in ("bank", "non_financial"):
        for role in EXTRACTION_ROLES:
            if role == OTHER_ROLE:
                continue
            plan = build_role_plan(role, company_type)
            if not plan.output_enums:  # None (=all) or empty (=unsupported)
                continue
            label = ROLE_LABELS.get(role, role)
            for enum in plan.output_enums:
                out.setdefault((company_type, str(enum).strip().upper()), set()).add(label)
    return out


def build_registry_export_workbook() -> io.BytesIO:
    """Build the consolidated registry xlsx; returns a seeked BytesIO buffer."""
    import openpyxl
    from openpyxl.styles import Font

    roles_map = _specialised_roles_by_ct_enum()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Реестр (свод)"
    ws.append(_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for profile_id in PROFILE_IDS:
        standard, company_type, language = _axes(profile_id)
        reg = registry_for_profile(profile_id)
        for m in reg["extractable"]:
            enum = str(m["enum"]).strip().upper()
            roles = ", ".join(sorted(roles_map.get((company_type, enum), set())))
            aliases = "; ".join((m.get("aliases_ru") or []) + (m.get("aliases_en") or []))
            ws.append([
                profile_id, standard, language, company_type, "извлекаемый",
                m.get("enum"), m.get("name_ru"), m.get("name_en"), "",
                m.get("value_basis") or "", m.get("period_type") or "",
                "Y" if m.get("formula_default_abs_behavior") else "",
                m.get("hint") or "", m.get("description") or "", aliases, roles,
            ])
        for c in reg["calculated"]:
            enum = str(c[0]).strip().upper()
            roles = ", ".join(sorted(roles_map.get((company_type, enum), set())))
            ws.append([
                profile_id, standard, language, company_type, "расчётный",
                c[0], c[1], "", c[2], "", "", "", "", "", "", roles,
            ])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
