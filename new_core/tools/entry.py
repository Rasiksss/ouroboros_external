"""Legacy in-process tool entry: schema plus a local callable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from new_core.tools.schema import ToolSchema


@dataclass
class ToolEntry:
    """Single tool descriptor."""

    name: str
    schema: ToolSchema
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 360
    # Optional legacy env prefix used by tool selection for first-prompt promotion.
    env_prefix: str | None = None

    # Runtime-metadata contract (#84). ``None`` means "unstated": the descriptor
    # builder falls back to the name-keyed core ToolSpec.
    safety_policy: str | None = None
    capability_class: str | None = None
    result_limit: int | None = None
    parallel_safe: bool | None = None
    retrieval_aliases: tuple[str, ...] = ()
    log_redacted_args: frozenset[str] = frozenset()

    def __post_init__(self):
        # Нормализуем schema: если это dict — преобразуем в ToolSchema
        if isinstance(self.schema, dict):
            self.schema = ToolSchema(**self.schema)
