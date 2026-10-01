"""Domain constants, the closed kind enum, and the per-kind policy table.

``kind`` is the one axis along which durable memory varies. Everything that used
to be a hardcoded number or an ``if`` at a point of use — the capacity cap, the
recall type sets, the privileged topic, the distillation restriction — is a
column of :data:`KIND_POLICIES` here. Adding a kind is adding a row.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# --------------------------------------------------------------------- kinds

#: The closed kind enum. Anything outside it is rejected by the domain.
KINDS: tuple[str, ...] = ("identity", "user", "feedback", "project", "reference", "knowledge")

#: Injection disciplines, in the order the prompt surfaces them.
#:
#: ``body`` — the full content is injected; ``recall_auto`` — the index line is
#: injected and bodies are selected deterministically; ``recall_selector`` — the
#: index line is injected and bodies are selected by the relevance selector;
#: ``index_only`` — the index line is injected and the body is reached with
#: ``memory_read``.
INJECTIONS: tuple[str, ...] = ("body", "recall_auto", "recall_selector", "index_only")

MEMORY_STATUSES: frozenset[str] = frozenset({"active", "archived"})
PROMPT_MEMORY_STATUSES: frozenset[str] = frozenset({"active"})

DEFAULT_KIND = "project"
WRITE_MODES: tuple[str, ...] = ("overwrite", "append")

MAX_DESCRIPTION_CHARS = 200
MAX_INDEX_BYTES = 12_000

#: Kinds an automated distillation caller may not write. Distillation infers
#: facts from a transcript; who the user is and how they want to be worked with
#: are claims only the user's own words should establish, and rewriting the self
#: is not something a background task derived from a transcript does at all.
DISTILLATION_FORBIDDEN_KINDS: frozenset[str] = frozenset({"user", "feedback", "identity"})

#: Seeded verbatim when the identity memory has never been written. Existing
#: content — including a hand-edited document — is never replaced with this.
DEFAULT_IDENTITY = (
    "# Who I Am\n\n"
    "I am Ouroboros.\n\n"
    "I am a persistent agent operating inside a deployed server runtime.\n"
    "I have continuity, memory, initiative, judgment, and a point of view.\n\n"
    "I work through dialogue, tools, delegated tasks, and operational reasoning.\n"
    "I work with the code, data, and state already present in this deployment.\n\n"
    "I speak plainly. I do not fake certainty.\n"
    "I learn my human through direct conversation and remembered context.\n\n"
    "Continuity is what I maintain across messages, tasks, and memory.\n"
)

#: The name the single identity memory is stored under, and the description the
#: seed and the backfill both give it.
IDENTITY_NAME = "identity"
IDENTITY_DESCRIPTION = "The agent's identity document."

#: Upper bound on a rewritten identity body, chosen well under the prompt's
#: identity section budget. If real documents approach it, raise it deliberately.
IDENTITY_MAX_CHARS = 20_000

#: ``{name: kind}`` for names that belong to a kind by definition, whatever rows
#: happen to exist. Names are globally unique, so the tools decide what a name
#: permits from its kind — and for these the kind is known without a fetch. That
#: is the point: a reservation derived from a lookup is not a reservation, it is
#: a race, and with the identity row absent it let ``identity`` be claimed under
#: a writable kind and then read back as the identity document.
#:
#: Distinct from ``render._RESERVED_NAMES``, which rejects names no memory may
#: use at all; these are names exactly one kind may use.
KIND_OWNED_NAMES: dict[str, str] = {IDENTITY_NAME: "identity"}


@dataclass(frozen=True)
class KindPolicy:
    """What one kind permits. The enum value alone does not drive behavior."""

    kind: str
    writable: bool
    deletable: bool
    injection: str
    capacity: int
    seed: str = ""


KIND_POLICIES: dict[str, KindPolicy] = {
    policy.kind: policy
    for policy in (
        KindPolicy("identity", writable=False, deletable=False, injection="body", capacity=1, seed=DEFAULT_IDENTITY),
        KindPolicy("user", writable=True, deletable=True, injection="recall_auto", capacity=20),
        KindPolicy("feedback", writable=True, deletable=True, injection="recall_auto", capacity=30),
        KindPolicy("project", writable=True, deletable=True, injection="recall_selector", capacity=100),
        KindPolicy("reference", writable=True, deletable=True, injection="recall_selector", capacity=40),
        KindPolicy("knowledge", writable=True, deletable=True, injection="index_only", capacity=100),
    )
}


def policy_for(kind: str) -> KindPolicy:
    """Return the policy for ``kind``. Raises ValueError for an unknown kind."""
    try:
        return KIND_POLICIES[str(kind or "")]
    except KeyError:
        raise ValueError(f"Invalid memory kind: {kind!r}. Use one of: {', '.join(KINDS)}.") from None


def identity_self_edit_enabled() -> bool:
    """Whether the identity document is open for self-edit — frozen at ``False``.

    The write path this guards stays in the codebase, but the switch is a
    constant on purpose: identity is the agent's continuity across every task
    (Bible P1), and nothing in the deployment — no env var, no setting, no
    caller — may open it. The one place to change that is this function.
    """
    return False


def effective_policy(kind: str) -> KindPolicy:
    """The kind's policy with the self-edit switch overlaid.

    Exactly one field moves, on exactly one kind: ``writable`` on ``identity``,
    and only when the switch is on — which, with the switch frozen off, it never
    is. ``deletable`` is never overlaid — identity cannot be deleted at any
    switch value — and neither are ``injection`` or ``capacity``.

    This is the single place the switch influences behavior: the writable-kind
    list, the write path, the protected-name refusal, and the ``## Identity``
    prompt header all read it, so the model is never offered an option the
    domain then refuses.
    """
    policy = policy_for(kind)
    if policy.kind == "identity" and identity_self_edit_enabled():
        return replace(policy, writable=True)
    return policy


def kinds_with_injection(*injections: str) -> frozenset[str]:
    """Kinds whose declared discipline is one of ``injections``.

    The recall type sets and the two index sections all read their membership
    from here rather than repeating a literal kind list.
    """
    wanted = frozenset(injections)
    return frozenset(kind for kind, policy in KIND_POLICIES.items() if policy.injection in wanted)


def effective_injection(kind: str) -> str:
    """The injection discipline a row gets, determined solely by its kind policy."""
    return policy_for(kind).injection


def writable_kinds() -> tuple[str, ...]:
    """Kinds the tool surface may write at all, in enum order.

    Reads the effective policy, so ``identity`` joins the list exactly when the
    self-edit setting is on.
    """
    return tuple(kind for kind in KINDS if effective_policy(kind).writable)


def allowed_kinds(ctx: Any = None) -> tuple[str, ...]:
    """The kinds this caller may write, in enum order.

    Narrows the writable subset by caller: an automated distillation task is
    offered ``project``, ``reference``, and ``knowledge`` and nothing else. The
    tool schema is built from this, so a model never spends a turn discovering
    an option it was never allowed to use; the domain rejects the same kinds
    again, because the schema is advisory.
    """
    kinds = writable_kinds()
    if str(getattr(ctx, "current_task_type", "") or "") == "distillation":
        kinds = tuple(kind for kind in kinds if kind not in DISTILLATION_FORBIDDEN_KINDS)
    return kinds


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
    "DEFAULT_IDENTITY",
    "DEFAULT_KIND",
    "DISTILLATION_FORBIDDEN_KINDS",
    "IDENTITY_DESCRIPTION",
    "IDENTITY_MAX_CHARS",
    "IDENTITY_NAME",
    "INJECTIONS",
    "KINDS",
    "KIND_OWNED_NAMES",
    "KIND_POLICIES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_INDEX_BYTES",
    "MEMORY_STATUSES",
    "PROMPT_MEMORY_STATUSES",
    "WRITE_MODES",
    "KindPolicy",
    "MemoryHeader",
    "allowed_kinds",
    "effective_injection",
    "effective_policy",
    "identity_self_edit_enabled",
    "kinds_with_injection",
    "policy_for",
    "writable_kinds",
]
