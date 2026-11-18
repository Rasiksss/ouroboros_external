"""Supported memory kinds and their capacity and recall policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------- kinds

#: The closed kind enum. Anything outside it is rejected by the domain.
KINDS: tuple[str, ...] = ("user", "feedback", "project", "reference", "knowledge")

MEMORY_STATUSES: frozenset[str] = frozenset({"active", "archived"})
PROMPT_MEMORY_STATUSES: frozenset[str] = frozenset({"active"})

DEFAULT_KIND = "project"
WRITE_MODES: tuple[str, ...] = ("overwrite", "append")

MAX_DESCRIPTION_CHARS = 200
MAX_INDEX_BYTES = 12_000

DISTILLATION_FORBIDDEN_KINDS: frozenset[str] = frozenset({"user", "feedback"})


@dataclass(frozen=True)
class KindPolicy:
    """What one kind permits. The enum value alone does not drive behavior."""

    kind: str
    injection: str
    capacity: int


KIND_POLICIES: dict[str, KindPolicy] = {
    policy.kind: policy
    for policy in (
        KindPolicy("user", injection="recall_auto", capacity=20),
        KindPolicy("feedback", injection="recall_auto", capacity=30),
        KindPolicy("project", injection="recall_selector", capacity=100),
        KindPolicy("reference", injection="recall_selector", capacity=40),
        KindPolicy("knowledge", injection="index_only", capacity=100),
    )
}


def policy_for(kind: str) -> KindPolicy:
    """Return the policy for ``kind``. Raises ValueError for an unknown kind."""
    try:
        return KIND_POLICIES[str(kind or "")]
    except KeyError:
        raise ValueError(f"Invalid memory kind: {kind!r}. Use one of: {', '.join(KINDS)}.") from None


def kinds_with_injection(*injections: str) -> frozenset[str]:
    """Kinds whose declared discipline is one of ``injections``.

    The recall type sets and the two index sections all read their membership
    from here rather than repeating a literal kind list.
    """
    wanted = frozenset(injections)
    return frozenset(kind for kind, policy in KIND_POLICIES.items() if policy.injection in wanted)


def allowed_kinds(ctx: Any = None) -> tuple[str, ...]:
    """The kinds this caller may write, in enum order.

    Narrows the writable subset by caller: an automated distillation task is
    offered ``project``, ``reference``, and ``knowledge`` and nothing else. The
    tool schema is built from this, so a model never spends a turn discovering
    an option it was never allowed to use; the domain rejects the same kinds
    again, because the schema is advisory.
    """
    kinds = KINDS
    if str(getattr(ctx, "current_task_type", "") or "") == "distillation":
        kinds = tuple(kind for kind in kinds if kind not in DISTILLATION_FORBIDDEN_KINDS)
    return kinds


@dataclass(frozen=True)
class RecalledMemory:
    """One memory selected by recall, with the times the prompt renders beside it.

    A named shape rather than the ``(content, first_recorded_at, updated_at)``
    triple this used to be: the same three values crossed four call frames
    positionally, and which float was which survived only in a docstring.
    """

    content: str
    first_recorded_at: float
    updated_at: float


@dataclass
class MemoryHeader:
    """Index-level view of one memory. Never carries the body."""

    name: str
    kind: str
    description: str
    updated_at: float
    first_recorded_at: float = 0.0
    source: str = ""
    status: str = "active"
    last_recalled_at: float = 0.0
    recall_count: int = 0


__all__ = [
    "DEFAULT_KIND",
    "DISTILLATION_FORBIDDEN_KINDS",
    "KINDS",
    "KIND_POLICIES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INDEX_BYTES",
    "MEMORY_STATUSES",
    "PROMPT_MEMORY_STATUSES",
    "WRITE_MODES",
    "KindPolicy",
    "MemoryHeader",
    "RecalledMemory",
    "allowed_kinds",
    "kinds_with_injection",
    "policy_for",
]
