"""Exceptions raised by the graph core."""

from __future__ import annotations


class GraphError(Exception):
    """Base class for all graph-core errors."""


class GraphValidationError(GraphError):
    """Raised by :meth:`Graph.build` when the topology is invalid."""


class GraphExecutionError(GraphError):
    """Raised during a run when execution cannot proceed.

    For example, a router returned the name of a node that does not exist.
    """


class StepBudgetExceededError(GraphError):
    """Raised when a run exceeds its ``max_steps`` budget without reaching END."""
