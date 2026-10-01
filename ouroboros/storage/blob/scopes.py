"""Storage scope constants (wiki StorageProvider layout)."""

from __future__ import annotations

SCOPE_MEMORY = "memory"
SCOPE_STATE = "state"
SCOPE_LOGS = "logs"
SCOPE_UPLOADS = "uploads"
SCOPE_A2A_TASKS = "a2a_tasks"

ALL_SCOPES = frozenset(
    {
        SCOPE_MEMORY,
        SCOPE_STATE,
        SCOPE_LOGS,
        SCOPE_UPLOADS,
        SCOPE_A2A_TASKS,
    }
)
