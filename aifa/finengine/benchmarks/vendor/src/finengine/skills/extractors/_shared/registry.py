"""Metric registry building blocks — re-exports + helpers for cells."""

from __future__ import annotations

from finengine.skills.metric_registry import (
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
)
from finengine.skills.metric_schema import registry_for_profile

__all__ = [
    "CALCULATED_BANK",
    "CALCULATED_NON_FINANCIAL",
    "EXTRACTABLE_BANK",
    "EXTRACTABLE_NON_FINANCIAL",
    "extractable_enum_set",
    "calculated_enum_set",
    "full_registry_enum_set",
    "registry_has_extractable",
    "registry_for_profile",
    "resolve_cell_registry",
]


def extractable_enum_set(extractable: list[dict]) -> set[str]:
    """{enum} for a list of registry rows; normalised to upper-case."""
    return {str(m.get("enum", "")).strip().upper() for m in extractable if m.get("enum")}


def calculated_enum_set(calculated: list[tuple]) -> set[str]:
    """First-position enum from calculated tuples; normalised."""
    return {str(m[0]).strip().upper() for m in calculated if m and m[0]}


def full_registry_enum_set(
    extractable: list[dict], calculated: list[tuple]
) -> set[str]:
    """Union of extractable + calculated enums — what a cell hands to
    bulk_extractor as `metric_enums` to override company_type-based
    detection while keeping the full registry scope intact."""
    return extractable_enum_set(extractable) | calculated_enum_set(calculated)


def registry_has_extractable(reg: dict, enum: str) -> bool:
    """Whether a resolved cell registry contains an extractable enum."""
    wanted = str(enum or "").strip().upper()
    return any(
        str(m.get("enum", "")).strip().upper() == wanted
        for m in reg.get("extractable", [])
    )


def resolve_cell_registry(
    profile_id: str, ctx_metric_enums: list[str] | None = None
) -> tuple[dict, set[str]]:
    """Resolve a cell's registry (shared base + its per-cell override file) and
    the enum set to extract.

    Returns ``(registry, enums)`` where ``registry`` is the dict from
    ``registry_for_profile`` (pass it to bulk_extractor as ``registry_override``)
    and ``enums`` is what to extract — the caller's explicit subset if given,
    otherwise the full resolved registry (extractable + calculated). With an
    empty/absent override this equals the old base behaviour exactly.
    """
    reg = registry_for_profile(profile_id)
    if ctx_metric_enums:
        enums = {str(e).strip().upper() for e in ctx_metric_enums if str(e).strip()}
    else:
        enums = {str(m["enum"]).strip().upper() for m in reg["extractable"]} | {
            str(c[0]).strip().upper() for c in reg["calculated"]
        }
    return reg, enums
