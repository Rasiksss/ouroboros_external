"""Repo-defined tools: the frozen module list under ``ouroboros/tools``."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.tools.catalog import spec_for_tool
from ouroboros.tools.spec import ToolSpec

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)

FROZEN_TOOL_MODULES = (
    "attachments",
    "calendar_resolve",
    "calculator",
    "compact_context",
    "core",
    "gigasearch",
    "memory_tools",
    "recent_tasks",
    "service.messaging",
    "service.memory",
    "service.orchestration",
    "skill_exec",
    "skill_provisioning",
    "skill_preflight",
    "skills",
    "tool_discovery",
    "vision",
    "browser",
)


def entry_from_decl(decl: ToolEntry | ToolSpec) -> ToolEntry:
    """Normalize a module's ``get_tools()`` element into a ``ToolEntry``."""
    if isinstance(decl, ToolEntry):
        return decl
    if not decl.schema or decl.handler is None:
        raise ValueError(f"tool spec {decl.name!r} is missing schema or handler")
    if not isinstance(decl.parallel, bool):
        # A truthy string like "false" would batch the tool against its own declaration.
        raise ValueError(f"tool spec {decl.name!r} parallel must be a bool")
    return ToolEntry(
        name=decl.name,
        schema=ToolSchema(**decl.schema),
        handler=decl.handler,
        is_code_tool=decl.is_code_tool,
        timeout_sec=decl.timeout_sec,
        env_prefix=decl.env_prefix,
        # Carry the spec's declared metadata: a decl-only spec has no row in
        # TOOL_SPECS, so anything dropped here is silently replaced by defaults.
        safety_policy=decl.safety_policy,
        capability_class=decl.capability_class,
        result_limit=decl.result_limit,
        parallel_safe=decl.parallel,
    )


def descriptor_from_entry(entry: ToolEntry, origin: str) -> ToolDescriptor:
    """Build a descriptor from a local entry.

    Policy the entry states itself wins (the #84 contract, applied by the
    canonical namespaced name); anything unstated falls back to
    ``spec_for_tool``, and an unknown name defaults to the safe ``check``.
    ToolEntry-returning builtin modules leave the fields unstated, so their
    policy still comes from TOOL_SPECS; a ToolSpec decl always states its
    fields, so for @tool-decorated modules the decorator wins over the table
    (an invariant test keeps the two sources equal).
    """
    spec = spec_for_tool(entry.name)
    return ToolDescriptor(
        schema=entry.schema,
        timeout_sec=entry.timeout_sec,
        origin=origin,
        surface=spec.surface,
        capability_class=entry.capability_class or spec.capability_class,
        safety_policy=entry.safety_policy or spec.safety_policy,
        is_code_tool=entry.is_code_tool,
        env_prefix=entry.env_prefix,
        result_limit=entry.result_limit if entry.result_limit is not None else spec.result_limit,
        parallel_safe=entry.parallel_safe if entry.parallel_safe is not None else spec.parallel,
        retrieval_aliases=entry.retrieval_aliases,
        log_redacted_args=entry.log_redacted_args,
    )


class BuiltinToolProvider(IToolProvider):
    """Tools exported by ``get_tools()`` in the frozen repo module list."""

    default_runtime = THREAD_RUNTIME

    def __init__(self, modules: Sequence[str] = FROZEN_TOOL_MODULES) -> None:
        self._modules = tuple(modules)
        self._entries: dict[str, ToolEntry] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}

    def refresh(self) -> None:
        entries: dict[str, ToolEntry] = {}
        for modname in self._modules:
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for decl in mod.get_tools():
                        entry = entry_from_decl(decl)
                        entries[entry.name] = entry
            except Exception:
                log.warning("Failed to load tool module %s", modname, exc_info=True)
        self._entries = entries
        self._descriptors = {name: descriptor_from_entry(entry, "builtin") for name, entry in entries.items()}

    def descriptors(self) -> Sequence[ToolDescriptor]:
        return list(self._descriptors.values())

    def get(self, name: str) -> ToolDescriptor | None:
        return self._descriptors.get(str(name or "").strip())

    def refuse_dispatch(self, name: str) -> str | None:
        """Nothing to withhold: a repo module either exports a tool or it does not.

        Whether a repo-defined tool may be *called* is policy — the excluded surface, heal
        mode — and that is decided by the gates above, on names this provider does resolve.
        """
        return None

    def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        entry = self._entries.get(str(name or "").strip())
        if entry is None:
            return f"⚠️ Unknown tool: {name}"
        try:
            return entry.handler(ctx, **args)
        except TypeError as exc:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {exc}"
        except Exception as exc:
            return f"⚠️ TOOL_ERROR ({name}): {exc}"

    def entries(self) -> dict[str, ToolEntry]:
        """The local entries behind these descriptors, for the ``ToolRegistry`` façade.

        Not part of ``IToolProvider``: only providers with a local callable have entries,
        and only the façade — deleted in the follow-up change — needs them.
        """
        return dict(self._entries)
