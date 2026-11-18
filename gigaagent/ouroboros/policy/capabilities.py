"""Derived views over the tool catalog."""

from __future__ import annotations

from ouroboros.tools.catalog import (
    DEFAULT_TOOL_RESULT_LIMIT as _DEFAULT_TOOL_RESULT_LIMIT,
    core_tool_names,
    meta_tool_names,
    read_only_parallel_tool_names,
    reviewed_mutative_tool_names,
    tool_result_limits,
    untruncated_tool_names,
)

CORE_TOOL_NAMES: frozenset[str] = core_tool_names()

# Meta-tools: always visible alongside core tools.
META_TOOL_NAMES: frozenset[str] = meta_tool_names()


READ_ONLY_PARALLEL_TOOLS: frozenset[str] = read_only_parallel_tool_names()

# Full outputs are semantic (review verdicts, advisory findings, status).
UNTRUNCATED_TOOL_RESULTS: frozenset[str] = untruncated_tool_names()

# Per-tool char caps; omitted tools use DEFAULT_TOOL_RESULT_LIMIT.
DEFAULT_TOOL_RESULT_LIMIT: int = _DEFAULT_TOOL_RESULT_LIMIT
TOOL_RESULT_LIMITS: dict[str, int] = tool_result_limits()

# Reviewed mutative tools must not end with ambiguous executor timeouts.
REVIEWED_MUTATIVE_TOOLS: frozenset[str] = reviewed_mutative_tool_names()
