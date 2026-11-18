"""Deployment-wide admission budgets: the seam and its single-process implementation."""

from new_core.coordination.interfaces import (
    BudgetTimeout,
    IConcurrencyBudget,
    ITaskStopConsumer,
    ITaskStopPublisher,
    Lease,
    TaskStopNotice,
    UnknownBudget,
)

__all__ = [
    "BudgetTimeout",
    "IConcurrencyBudget",
    "ITaskStopConsumer",
    "ITaskStopPublisher",
    "Lease",
    "TaskStopNotice",
    "UnknownBudget",
]
