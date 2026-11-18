"""Canonical Target/ProducerResolver/PlanGraph/GraphExecutor contracts.

The former YAML ``steps:`` runner was removed in Slice 14: a manually ordered
skill specification is an orchestration source competing with ``PlanGraph``.
Import concrete graph modules directly so this package cannot grow a second
facade that silently chooses another execution mechanism.
"""
