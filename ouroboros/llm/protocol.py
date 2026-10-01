"""Provider adapter protocol for the headless LLM registry."""

from __future__ import annotations

from typing import Any, Protocol

from new_core.tools.schema import ToolSchema
from ouroboros.llm.models import ModelRef
from ouroboros.llm.targets import RemoteTarget


class ProviderAdapter(Protocol):
    provider_id: str

    def close(self) -> None: ...

    def supports(self, ref: ModelRef) -> bool: ...

    def resolve_target(self, ref: ModelRef) -> RemoteTarget: ...

    def chat(
        self,
        target: RemoteTarget | dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSchema] | None,
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: float | None,
        no_proxy: bool,
        read_timeout_sec: float | None = None,
        phase_cap_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...
