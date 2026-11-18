"""Storage scope constants (wiki StorageProvider layout)."""

from __future__ import annotations

SCOPE_MEMORY = "memory"
SCOPE_STATE = "state"
SCOPE_UPLOADS = "uploads"
SCOPE_A2A_TASKS = "a2a_tasks"
SCOPE_WORKSTATION = "workstation"

ALL_SCOPES = frozenset(
    {
        SCOPE_MEMORY,
        SCOPE_STATE,
        SCOPE_UPLOADS,
        SCOPE_A2A_TASKS,
        SCOPE_WORKSTATION,
    }
)

RETIRED_SCOPES: frozenset[str] = frozenset({"logs"})

#: The scopes whose physical keys carry an owner segment. One member, and the exclusions
#: named below have a reason rather than an omission:
#:
#: - ``uploads`` — chat attachments, published artifacts and task artifacts: the only
#:   scope holding per-person content.
#: - ``state`` — instance state, including private extension and companion state. Not per-person.
#: - ``memory`` — no production writer through the provider; durable memory lives in the
#:   runtime database keyed by owner and agent.
#: - ``a2a_tasks`` — no production writer through the provider: the task store keeps its
#:   rows in the runtime database, and A2A is refused in multi-owner mode.
#:
#: A non-member scope produces the same key for any two owners; the required
#: ``owner_scope`` argument is plumbing there, not an isolation claim.
OWNER_KEYED_SCOPES = frozenset({SCOPE_UPLOADS})
