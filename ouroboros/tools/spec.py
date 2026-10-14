"""Small declarative contract for LLM-callable tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeVar, cast

ToolCategory = Literal[
    "task",
    "service",
    "memory",
    "execution",
    "network",
    "extension",
]
ToolSurface = Literal["core", "discoverable", "excluded"]
ToolSafetyPolicy = Literal["skip", "check", "check_conditional"]
ToolExecutionMode = Literal["direct", "worker"]
CapabilityClass = Literal[
    "read_only",
    "mutative",
    "skill",
    "shell",
    "external_network",
    "control",
    "git",
    "external_shell",
]

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: ToolCategory = "task"
    surface: ToolSurface = "discoverable"
    safety_policy: ToolSafetyPolicy = "check"
    parallel: bool = False
    result_limit: int | None = None
    untruncated_result: bool = False
    preserve_result: bool = False
    evidence_result: bool = False
    result_receipt: bool = True
    durable_result_artifact: bool = False
    stateful: bool = False
    reviewed_mutative: bool = False
    timeout_sec: int = 360
    execution: ToolExecutionMode = "direct"
    env_prefix: str | None = None
    is_code_tool: bool = False
    capability_class: CapabilityClass = "read_only"
    schema: dict[str, Any] | None = None
    handler: Callable[..., str] | None = None

    @property
    def visibility(self) -> ToolSurface:
        """Backward-compatible alias while older modules move to ``surface``."""

        return self.surface

    def bind(
        self,
        *,
        schema: dict[str, Any],
        handler: Callable[..., str],
        timeout_sec: int | None = None,
        env_prefix: str | None = None,
        is_code_tool: bool | None = None,
    ) -> ToolSpec:
        return replace(
            self,
            schema=schema,
            handler=handler,
            timeout_sec=self.timeout_sec if timeout_sec is None else int(timeout_sec),
            env_prefix=self.env_prefix if env_prefix is None else env_prefix,
            is_code_tool=self.is_code_tool if is_code_tool is None else bool(is_code_tool),
        )


def tool(
    *,
    name: str,
    schema: dict[str, Any],
    category: ToolCategory = "task",
    surface: ToolSurface = "discoverable",
    safety_policy: ToolSafetyPolicy = "check",
    parallel: bool = False,
    result_limit: int | None = None,
    timeout_sec: int = 360,
    env_prefix: str | None = None,
    execution: ToolExecutionMode = "direct",
    capability_class: CapabilityClass = "read_only",
) -> Callable[[F], F]:
    """Attach a ``ToolSpec`` to a handler without changing the function."""

    def _decorator(fn: F) -> F:
        spec = ToolSpec(
            name=name,
            category=category,
            surface=surface,
            safety_policy=safety_policy,
            parallel=parallel,
            result_limit=result_limit,
            timeout_sec=timeout_sec,
            env_prefix=env_prefix,
            execution=execution,
            capability_class=capability_class,
            schema=schema,
            handler=cast(Callable[..., str], fn),
        )
        fn.__ouroboros_tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return _decorator


def spec_from_callable(fn: Callable[..., Any]) -> ToolSpec:
    spec = getattr(fn, "__ouroboros_tool_spec__", None)
    if not isinstance(spec, ToolSpec):
        raise TypeError(f"{getattr(fn, '__name__', fn)!r} is not decorated with @tool")
    return spec
