"""The process's budgets, built once by the composition root.

Code that cannot be handed a budget as an argument reads it here — the model client is
reached through forty-odd construction sites, and threading a budget through all of them
would put the seam in every signature rather than at the one place that knows the limits.
Until the composition root installs one, the budgets are the in-process implementation at
its documented defaults: an instance that never composes is bounded rather than unbounded.
"""

from __future__ import annotations

from new_core.coordination.inprocess import InProcessBudget
from new_core.coordination.interfaces import IConcurrencyBudget

__all__ = [
    "MODEL_CALLS",
    "TOOL_BODIES",
    "ConcurrencyBudgetFactory",
    "concurrency_budgets",
    "default_budget_limits",
]

#: Model calls in flight across every run this instance holds.
MODEL_CALLS = "model_calls"
#: Tool bodies executing at once across every run. A per-round allowance is a share of it.
TOOL_BODIES = "tool_bodies"

#: What an uncomposed process runs on: one slot's worth of each, which is what a
#: single-lane instance had before any of this was configurable.
_FALLBACK_LIMITS = {MODEL_CALLS: 2, TOOL_BODIES: 8}


def default_budget_limits(*, worker_slots: int, llm_max_concurrency: int) -> dict[str, int]:
    """The limits an instance of this size runs on, in one place with their relationship.

    The model-call budget sits above the slot count: every run may have a call in flight,
    and the supervisor's own diagnostic call must not have to wait for one of them to
    finish. Tool bodies are a multiple of the slots, because a round admits several at
    once and a body abandoned at its deadline keeps its thread for the grace period.
    """
    slots = max(1, int(worker_slots or 1))
    return {
        MODEL_CALLS: max(slots + 1, int(llm_max_concurrency or 0)),
        TOOL_BODIES: slots * 8,
    }


class ConcurrencyBudgetFactory:
    """Holds the process's budgets, replaced once at composition."""

    def __init__(self) -> None:
        self._budgets: IConcurrencyBudget = InProcessBudget(dict(_FALLBACK_LIMITS))

    def initialize(self, budgets: IConcurrencyBudget) -> None:
        self._budgets = budgets

    def reset_for_tests(self) -> None:
        self._budgets = InProcessBudget(dict(_FALLBACK_LIMITS))

    @property
    def budgets(self) -> IConcurrencyBudget:
        return self._budgets


concurrency_budgets = ConcurrencyBudgetFactory()
