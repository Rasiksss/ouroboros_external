"""Extractor contract shared by every cell of the matrix.

The matrix has 8 cells: ifrs_nf_ru, ifrs_bank_ru, us_gaap_nf_en,
us_gaap_bank_en, ifrs_nf_en, ifrs_bank_en, ind_as_nf_en,
ind_gaap_bank_en. Each cell lives in its own module (_ifrs_nf_ru.py,
_ifrs_bank_ru.py, …) and exposes a single
async generator `stream(ctx) -> AsyncGenerator[dict, None]`.

Events yielded by `stream` are plain dicts so api/main.py can pipe them
into SSE / journal without serialization gymnastics. The known event
shapes are documented in `EVENT_TYPES` below — they match what the
current bulk_extractor pipeline already emits, so the UI keeps working
during rollout.

The dispatcher `get_extractor(profile_id)` routes a classified document
to the right cell. Unknown or explicitly unregistered profiles return
`None`, allowing the API's emergency rollback path to take over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Protocol,
    runtime_checkable,
)


# ── Event types yielded by every Extractor (informational; not enforced) ─

EVENT_TYPES: tuple[str, ...] = (
    "start",        # company_name, period_current, period_prev, periods, total_extractable
    "progress",     # enum, name_ru — current metric in flight
    "metric",       # enum, name_ru, value_current, value_prev, period_values, unit, source, page
    "segments",     # one or more segment rows for the previous `metric`
    "tokens",       # tokens_in/out/cached + cost_usd
    "stage",        # stage:str, message:str — coarse phase indicator
    "done",         # company_type, company_name, period_*, _diag
    "error",        # message:str — terminal
)


# ── What a cell receives ────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtractContext:
    """Everything a cell needs to run.

    Keep this flat — extractors should not need extra wiring beyond
    these fields. If a future cell needs more (e.g. a custom retriever),
    add an optional field here and ignore it in cells that don't care.
    """

    doc_id: str
    pdf_text: str                       # already-OCR'd .txt sidecar
    company_type: str                   # "non_financial" | "bank" | "unknown"

    # Tuning knobs (carried verbatim from the HTTP query string).
    model_id: str | None = None
    metric_enums: list[str] | None = None    # None → use cell's default registry
    role: str = ""
    include_segments: bool = True
    include_consolidated_group: bool = False
    include_top_borrowers: bool = False
    segment_mode: str = "extended"           # "core" | "extended"
    segment_context_mode: str = "full"       # "full" | "focused"
    limit: int | None = None

    # Backref to legacy infra. Cells should prefer pdf_text + their
    # own registry; index is here only for cells that still want FAISS.
    index: Any = None

    # Diagnostics / debug.
    debug_dir: str | None = None
    profile_reason: str = ""             # classifier reason, for logs
    extra: dict[str, Any] = field(default_factory=dict)


# ── The contract a cell must satisfy ────────────────────────────────────

ExtractStream = Callable[[ExtractContext], AsyncGenerator[dict, None]]


@runtime_checkable
class Extractor(Protocol):
    """A cell is any module exposing this top-level callable.

    Using a Protocol (not ABC) keeps cells as plain modules — no class
    boilerplate. `isinstance(mod, Extractor)` works for runtime checks
    thanks to runtime_checkable.
    """

    stream: ExtractStream


# ── Dispatch ────────────────────────────────────────────────────────────

# Registered cells. Cells fill themselves in by calling register() at import
# time; every supported profile is registered by extractors/__init__.py.
_REGISTRY: dict[str, ExtractStream | None] = {
    "ifrs_nf_ru": None,
    "ifrs_bank_ru": None,
    "us_gaap_nf_en": None,
    "us_gaap_bank_en": None,
    "ifrs_nf_en": None,
    "ifrs_bank_en": None,
    "ind_as_nf_en": None,
    "ind_gaap_bank_en": None,
}


def register(profile_id: str, stream_fn: ExtractStream) -> None:
    if profile_id not in _REGISTRY:
        raise ValueError(f"Unknown profile_id: {profile_id}")
    _REGISTRY[profile_id] = stream_fn


def get_extractor(profile_id: str) -> ExtractStream | None:
    """Returns the cell's stream() if registered, else None.

    None is reserved for an unknown or explicitly unregistered profile.
    """
    return _REGISTRY.get(profile_id)


def registered_profiles() -> tuple[str, ...]:
    """Sorted list of profile_ids whose cells are wired up."""
    return tuple(sorted(p for p, fn in _REGISTRY.items() if fn is not None))


__all__ = [
    "EVENT_TYPES",
    "ExtractContext",
    "Extractor",
    "ExtractStream",
    "get_extractor",
    "register",
    "registered_profiles",
]
