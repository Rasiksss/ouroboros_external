"""Frozen tool-module ABI: ToolEntry shape and get_tools() signature."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolEntryProtocol(Protocol):
    """Structural contract for the public ToolEntry descriptor fields."""

    name: str
    schema: dict[str, Any]
    handler: Callable[..., str]
    is_code_tool: bool
    timeout_sec: int
    # Runtime-metadata contract (#84); None/empty means "unstated".
    safety_policy: str | None
    capability_class: str | None
    result_limit: int | None
    parallel_safe: bool | None
    retrieval_aliases: tuple[str, ...]
    log_redacted_args: frozenset[str]


class GetToolsProtocol(Protocol):
    """Callable contract for ``get_tools()`` exported by every tools module."""

    def __call__(self) -> list[ToolEntryProtocol]: ...


__all__ = ["GetToolsProtocol", "ToolEntryProtocol"]
