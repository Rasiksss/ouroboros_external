from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from finengine.roles.access import (
    ALL_EXTRACTION_ROLE,
    EXTRACTION_ROLES,
    OTHER_ROLE,
    ROLE_LABELS,
    normalize_extraction_role,
)
from finengine.skills.metric_registry import (
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
)


LAYOUTS_PATH = Path(__file__).with_name("layouts.json")
TOP_BORROWERS_ENUM = "TOP_BORROWERS"

_PSEUDO_DEPS: dict[str, set[str]] = {
    "SALES_REVENUE_LTM": {"SALES_REVENUE"},
    "EBITDA_LTM": {"EBITDA"},
    "NET_INCOME_LTM": {"NET_INCOME"},
}


@dataclass(frozen=True)
class RolePlan:
    role: str
    role_label: str
    company_type: str
    format: str
    config: dict[str, Any] | None
    extraction_enums: set[str] | None
    output_enums: set[str] | None

    @property
    def is_all(self) -> bool:
        return self.role == ALL_EXTRACTION_ROLE


@dataclass(frozen=True)
class RoleSegmentPolicy:
    include_segments: bool
    segment_mode: str = "core"
    segment_context_mode: str = "full"


@lru_cache(maxsize=1)
def layouts() -> dict[str, Any]:
    return json.loads(LAYOUTS_PATH.read_text(encoding="utf-8-sig"))


def canonical_company_type(company_type: str | None) -> str:
    return "bank" if (company_type or "").strip().lower() == "bank" else "non_financial"


def _registry(
    company_type: str,
) -> tuple[set[str], dict[str, tuple[str, str, str, list[str]]], dict[str, str]]:
    if canonical_company_type(company_type) == "bank":
        extractable = EXTRACTABLE_BANK
        calculated = CALCULATED_BANK
    else:
        extractable = EXTRACTABLE_NON_FINANCIAL
        calculated = CALCULATED_NON_FINANCIAL

    extractable_enums = {str(m.get("enum", "")).strip().upper() for m in extractable if m.get("enum")}
    calc_by_enum = {str(m[0]).strip().upper(): m for m in calculated if m and m[0]}
    return extractable_enums | set(calc_by_enum), calc_by_enum, {}


def _registry_for_profile_id(
    profile_id: str,
) -> tuple[set[str], dict[str, tuple[str, str, str, list[str]]], dict[str, str]]:
    """Same as `_registry` but from a CELL's own per-cell registry, so the role
    plan respects metrics a cell added/dropped (not just the company_type base).
    Also returns ``role_tags`` (enum -> role) for user-uploaded overlay rows."""
    from finengine.skills.metric_schema import registry_for_profile

    reg = registry_for_profile(profile_id)
    extractable_enums = {str(m["enum"]).strip().upper() for m in reg["extractable"]}
    calc_by_enum = {str(c[0]).strip().upper(): c for c in reg["calculated"]}
    role_tags = {
        str(e).strip().upper(): str(r).strip().lower()
        for e, r in (reg.get("role_tags") or {}).items()
    }
    return extractable_enums | set(calc_by_enum), calc_by_enum, role_tags


def _row_enum(row: dict[str, Any]) -> str | None:
    enum = str(row.get("enum") or "").strip().upper()
    return enum or None


def _iter_layout_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for section_key in ("rows", "calculated_rows")
        for row in (config.get(section_key) or [])
        if isinstance(row, dict)
    ]


def _layout_output_enums(config: dict[str, Any]) -> set[str]:
    fmt = str(config.get("format") or "")
    out: set[str] = set()
    for row in _iter_layout_rows(config):
        enum = _row_enum(row)
        if not enum:
            continue
        if fmt in {"dgr", "mglob"} and not row.get("is_available"):
            continue
        out.add(enum)
    return out


def _expand_deps(
    enum: str,
    *,
    available_enums: set[str],
    calc_by_enum: dict[str, tuple[str, str, str, list[str]]],
    seen: set[str] | None = None,
) -> set[str]:
    enum = enum.strip().upper()
    seen = seen or set()
    if enum in seen:
        return set()
    seen.add(enum)

    deps = {d.strip().upper() for d in _PSEUDO_DEPS.get(enum, set()) if d.strip()}
    calc = calc_by_enum.get(enum)
    if calc:
        deps.update(str(d).strip().upper() for d in (calc[3] or []) if str(d).strip())

    out: set[str] = set()
    for dep in deps:
        if dep not in available_enums:
            continue
        out.add(dep)
        out.update(_expand_deps(dep, available_enums=available_enums, calc_by_enum=calc_by_enum, seen=seen))
    return out


def get_role_config(role: str | None, company_type: str | None) -> dict[str, Any] | None:
    normalized = normalize_extraction_role(role)
    if normalized == ALL_EXTRACTION_ROLE:
        return None
    role_cfg = (layouts().get("roles") or {}).get(normalized) or {}
    formats = role_cfg.get("formats") or {}
    return formats.get(canonical_company_type(company_type))


def role_supported_for_company_type(role: str | None, company_type: str | None) -> bool:
    normalized = normalize_extraction_role(role)
    if normalized in {ALL_EXTRACTION_ROLE, OTHER_ROLE}:
        return True
    if normalized not in EXTRACTION_ROLES:
        return False
    cfg = get_role_config(normalized, company_type)
    return bool(cfg and _layout_output_enums(cfg))


def role_should_include_segments(role: str | None, company_type: str | None) -> bool:
    return role_segment_policy(role, company_type).include_segments


def role_segment_policy(role: str | None, company_type: str | None) -> RoleSegmentPolicy:
    """Segment extraction policy for role-driven product runs.

    Role-based extraction intentionally asks only for core business
    breakdowns and sends the full document to the segment stage. Extended
    cross-breakdowns remain available in benchmark/debug flows, but are not
    part of the role product contract.
    """

    normalized = normalize_extraction_role(role)
    company = canonical_company_type(company_type)
    include = normalized == OTHER_ROLE or (
        company == "non_financial" and normalized in {"die", "cib"}
    )
    return RoleSegmentPolicy(
        include_segments=include,
        segment_mode="core",
        segment_context_mode="full",
    )


def role_should_include_consolidated_group(role: str | None, company_type: str | None) -> bool:
    """Only the generic "other" role receives group composition rows."""
    return normalize_extraction_role(role) == OTHER_ROLE


def role_should_include_top_borrowers(role: str | None, company_type: str | None) -> bool:
    """Bank outputs that expose largest-industry rows need the hidden mini-stage."""
    return normalize_extraction_role(role) in {"dgr", OTHER_ROLE} and canonical_company_type(company_type) == "bank"


def build_role_plan(
    role: str | None, company_type: str | None, profile_id: str | None = None
) -> RolePlan:
    normalized = normalize_extraction_role(role)
    company = canonical_company_type(company_type)
    if normalized in {ALL_EXTRACTION_ROLE, OTHER_ROLE}:
        return RolePlan(
            role=normalized,
            role_label=ROLE_LABELS[normalized],
            company_type=company,
            format="generic",
            config=None,
            extraction_enums=None,
            output_enums=None,
        )

    cfg = get_role_config(normalized, company)
    if not cfg:
        return RolePlan(
            role=normalized,
            role_label=ROLE_LABELS.get(normalized, normalized),
            company_type=company,
            format="unsupported",
            config=None,
            extraction_enums=set(),
            output_enums=set(),
        )

    available_enums, calc_by_enum, upload_role_tags = (
        _registry_for_profile_id(profile_id) if profile_id else _registry(company)
    )
    layout_enums = _layout_output_enums(cfg)
    output_enums = {e for e in layout_enums if e in available_enums}

    extraction_enums = set(output_enums)
    for enum in layout_enums:
        if enum in available_enums:
            extraction_enums.update(
                _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
            )
        elif enum in _PSEUDO_DEPS:
            for dep in _PSEUDO_DEPS[enum]:
                if dep in available_enums:
                    extraction_enums.add(dep)
                    extraction_enums.update(
                        _expand_deps(dep, available_enums=available_enums, calc_by_enum=calc_by_enum)
                    )

    # User-uploaded, role-tagged metrics extend the uploader's role plan only —
    # they are not in layouts.json, so other roles' layouts stay untouched.
    for enum, tag in upload_role_tags.items():
        if tag != normalized or enum not in available_enums:
            continue
        output_enums.add(enum)
        extraction_enums.add(enum)
        extraction_enums.update(
            _expand_deps(enum, available_enums=available_enums, calc_by_enum=calc_by_enum)
        )

    if (
        role_should_include_top_borrowers(normalized, company)
        and TOP_BORROWERS_ENUM in available_enums
    ):
        extraction_enums.add(TOP_BORROWERS_ENUM)

    return RolePlan(
        role=normalized,
        role_label=ROLE_LABELS.get(normalized, normalized),
        company_type=company,
        format=str(cfg.get("format") or ""),
        config=cfg,
        extraction_enums=extraction_enums,
        output_enums=output_enums,
    )
