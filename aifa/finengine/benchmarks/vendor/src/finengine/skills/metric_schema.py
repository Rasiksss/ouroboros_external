"""Per-cell metric registries — one self-contained registry per matrix cell.

Each of the 8 cells (profile_id) owns its COMPLETE metric registry in
``src/finengine/data/registry/<profile_id>.json``::

    {"extractable": [{enum, name_ru, name_en, aliases_ru | aliases_en (по языку
                      ячейки), description, hint, standard, value_basis,
                      period_type, formula_default_abs_behavior,
                      document_types}, ...],
     "calculated":  [{enum, name_ru, formula, deps}, ...]}

There is **no shared base**: a cell's registry is exactly its own file. Teams
edit their own cell's file independently (git/PR), in parallel, without
touching anyone else's. The files were seeded as copies of the company_type
base (the 3 NF cells identical, the 3 bank cells identical), so behaviour is
unchanged until a cell diverges. No DB (см. project_backlog.md → "Per-cell
разбивка реестра метрик").

Trade-off accepted by design: a metric common to several cells is duplicated
across their files, so a common change must be applied to each cell's file.

``metrics.json`` stays as the registry for the **non-cell** arches (consensus
/ AIFA baseline / role scope) and as the one-time seed for these files — it
does NOT regenerate them (that would clobber per-cell edits).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from finengine.config import settings

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "data" / "registry"


def _overlay_dir() -> Path:
    """Runtime overlay dir in the writable data volume. User-uploaded metric
    additions (role-tagged) live here and are merged on top of the git-shipped
    per-cell base — so uploads take effect without a redeploy and are per-env."""
    return Path(settings.data_dir) / "registry_overlays"


def _apply_overlay(profile_id: str, data: dict) -> dict:
    """Merge ``data/registry_overlays/<profile>.json`` onto the base per-cell
    registry: overlay rows override the base by enum and append new enums.
    Best-effort — a missing/corrupt overlay leaves the base untouched."""
    path = _overlay_dir() / f"{profile_id}.json"
    if not path.exists():
        return data
    try:
        overlay = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return data
    for key in ("extractable", "calculated"):
        ov_rows = overlay.get(key) or []
        if not ov_rows:
            continue
        base_rows = list(data.get(key, []))
        pos = {r["enum"]: i for i, r in enumerate(base_rows)}
        for r in ov_rows:
            e = r.get("enum")
            if e in pos:
                base_rows[pos[e]] = r
            else:
                base_rows.append(r)
        data[key] = base_rows
    return data

PROFILE_IDS: tuple[str, ...] = (
    "ifrs_nf_ru",
    "ifrs_bank_ru",
    "us_gaap_nf_en",
    "us_gaap_bank_en",
    "ifrs_nf_en",
    "ifrs_bank_en",
    "ind_as_nf_en",
    "ind_gaap_bank_en",
)

VALID_PERIOD_TYPES = {"", "point", "duration"}
VALID_METRIC_DOCUMENT_TYPES = {
    "financial_report",
    "press_release",
    "transcript",
    "presentation",
    "equity_research",
}


class MetricDef(BaseModel):
    """One extractable metric row. ``extra=forbid`` so a typo in a cell file
    (e.g. ``hnit``) fails loudly instead of silently doing nothing."""

    model_config = ConfigDict(extra="forbid")

    enum: str
    name_ru: str = ""
    name_en: str = ""
    aliases_ru: list[str] = Field(default_factory=list)
    aliases_en: list[str] = Field(default_factory=list)
    description: str = ""
    hint: str = ""
    standard: str = ""
    value_basis: str | None = None
    period_type: str = ""
    formula_default_abs_behavior: bool = False
    # Missing scope preserves the pre-classifier behaviour: the metric is
    # available only for financial reports. Non-report sources are opt-in.
    document_types: list[str] = Field(default_factory=lambda: ["financial_report"])
    # Provenance / role tag — set on user-uploaded overlay rows; "" for the
    # git-shipped base rows. layouts.json stays the scope for base rows;
    # rows with source="user_upload" extend the tagged role's plan
    # (roles/scope.py reads them via ``role_tags``).
    role: str = ""
    source: str = ""


class CalcDef(BaseModel):
    """One calculated (derived) metric row."""

    model_config = ConfigDict(extra="forbid")

    enum: str
    name_ru: str = ""
    name_en: str = ""
    formula: str = ""
    deps: list[str] = Field(default_factory=list)
    hint: str = ""
    role: str = ""
    source: str = ""


def _company_type_for_profile(profile_id: str) -> str:
    """Cell id carries the company type: ``*_bank_*`` → bank, else NF."""
    return "bank" if "_bank_" in (profile_id or "") else "non_financial"


def registry_for_profile(profile_id: str) -> dict[str, Any]:
    """Load a cell's OWN complete registry file and validate every row.

    Returns ``extractable`` rows (dicts) + ``calculated`` as
    ``(enum, name_ru, formula, deps)`` tuples — the shape the extraction
    pipeline consumes — plus ``company_type``.
    """
    path = _REGISTRY_DIR / f"{profile_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data = _apply_overlay(profile_id, data)

    role_tags: dict[str, str] = {}
    name_en_by_enum: dict[str, str] = {}

    extractable: list[dict] = []
    for row in data.get("extractable", []):
        metric = MetricDef(**row)
        if metric.period_type not in VALID_PERIOD_TYPES:
            raise ValueError(
                f"{profile_id}/{metric.enum}: bad period_type {metric.period_type!r}"
            )
        if not metric.document_types:
            raise ValueError(f"{profile_id}/{metric.enum}: document_types is empty")
        invalid_document_types = set(metric.document_types) - VALID_METRIC_DOCUMENT_TYPES
        if invalid_document_types:
            raise ValueError(
                f"{profile_id}/{metric.enum}: bad document_types "
                f"{sorted(invalid_document_types)!r}"
            )
        if len(metric.document_types) != len(set(metric.document_types)):
            raise ValueError(
                f"{profile_id}/{metric.enum}: duplicate values in document_types"
            )
        extractable.append(metric.model_dump())
        if metric.name_en:
            name_en_by_enum[metric.enum] = metric.name_en
        if metric.source == "user_upload" and metric.role:
            role_tags[metric.enum] = metric.role

    calculated: list[tuple] = []
    for row in data.get("calculated", []):
        calc = CalcDef(**row)
        calculated.append((calc.enum, calc.name_ru, calc.formula, calc.deps))
        if calc.name_en:
            name_en_by_enum[calc.enum] = calc.name_en
        if calc.source == "user_upload" and calc.role:
            role_tags[calc.enum] = calc.role

    return {
        "extractable": extractable,
        "calculated": calculated,
        "company_type": _company_type_for_profile(profile_id),
        "name_en_by_enum": name_en_by_enum,
        # enum -> role for user-uploaded rows only; roles/scope.py extends the
        # tagged role's plan with these (base rows are scoped by layouts.json).
        "role_tags": role_tags,
    }


def validate_all_registries() -> dict[str, int]:
    """Load + validate all per-cell registry files. Returns
    ``{profile_id: extractable_count}``; raises on the first bad row."""
    return {pid: len(registry_for_profile(pid)["extractable"]) for pid in PROFILE_IDS}
