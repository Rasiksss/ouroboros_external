"""Tool provider test doubles extracted from test_tool_dispatch."""

from __future__ import annotations

from typing import Any

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema


class RecordingProvider(IToolProvider):
    """A provider that records every execute it is asked to perform."""

    default_runtime = THREAD_RUNTIME

    def __init__(self, name: str, origin: str, *, skips_llm_safety: bool = False, safety_policy: str = "check") -> None:
        self._desc = ToolDescriptor(
            schema=ToolSchema(name=name, description="d", parameters={}),
            timeout_sec=1,
            origin=origin,
            surface="discoverable",
            capability_class="read_only",
            safety_policy=safety_policy,
            skips_llm_safety=skips_llm_safety,
        )
        self.calls: list[tuple[Any, str, dict]] = []

    def refresh(self) -> None:
        return

    def descriptors(self, scope):
        return [self._desc]

    def get(self, name: str, scope):
        return self._desc if name == self._desc.name else None

    def refuse_dispatch(self, name: str, scope) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        self.calls.append((ctx, name, dict(args)))
        return f"ran {name}"
