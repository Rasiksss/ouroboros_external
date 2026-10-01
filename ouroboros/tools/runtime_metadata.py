"""One frozen per-tool runtime metadata snapshot, resolved once per tool call.

The agent loop resolves this value object when a tool call is dispatched and pins it into
the execution result. Logging, truncation, parallel scheduling, and working retrieval all
read the pinned snapshot instead of re-resolving live registries mid-flight, so an
extension unloaded after the snapshot cannot change redaction or truncation behavior for
calls already in flight.

Almost every answer comes straight off the owning provider's :class:`ToolDescriptor` —
the provider is the only thing that knows what its tools declared, and the catalog is the
only thing that knows which provider owns a name. What is left for this module is the two
questions a descriptor cannot answer, because in both cases there *is* no descriptor:

- a name the catalog does not resolve but the static policy table still lists. The retired
  ``repo_read`` and friends are metadata-only tombstones with no handler behind them; they
  must still resolve to their declared caps rather than to bare defaults.
- an extension-shaped name with no live tool behind it. Redaction has to fail *closed*
  there — omit every argument value — and "unavailable" is a state a descriptor cannot
  represent, since a provider with nothing to offer returns ``None``.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING

from ouroboros.tools.catalog import DEFAULT_TOOL_RESULT_LIMIT, RESULT_LIMIT_HARD_MAX, TOOL_SPECS

if TYPE_CHECKING:
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.interfaces import ToolDescriptor

__all__ = ["RESULT_LIMIT_HARD_MAX", "ResolvedToolRuntimeMetadata", "resolve_tool_runtime_metadata"]

log = logging.getLogger(__name__)

# Conservative timeout for names without any live metadata (extension-shaped
# names whose skill is unloaded, unknown tools): execution fails fast anyway.
_DEFAULT_TIMEOUT_SEC = 60


@dataclass(frozen=True, slots=True)
class ResolvedToolRuntimeMetadata:
    """Frozen per-tool runtime facts, pinned at tool-call resolve time.

    ``log_redacted_args`` is ``None`` when the name is extension-shaped but the
    tool's metadata is unavailable (unloaded or lookup failed); consumers must
    then fail closed and omit every argument value from logs.
    """

    stable_name: str
    retrieval_aliases: tuple[str, ...]
    result_limit: int
    timeout_sec: int
    parallel_safe: bool
    log_redacted_args: frozenset[str] | None
    capability_class: str = "read_only"


def _extension_loader() -> ModuleType:
    """Load the extension registry lazily to avoid its skill-exec import cycle."""
    return importlib.import_module("ouroboros.skills.extensions.loader")


def _extension_shaped_tool_name(name: str) -> bool:
    """Return whether the name can only refer to an extension tool."""
    if "." in name:
        return True  # ``skill.tool`` alias form
    try:
        prefix = str(_extension_loader()._EXTENSION_NAME_PREFIX)
    except Exception:
        # Loader import failed: keep the shape check working with the literal
        # prefix so extension-shaped names still fail closed.
        prefix = "ext_"
    return name.startswith(prefix)


def _fail_closed_metadata(name: str) -> ResolvedToolRuntimeMetadata:
    """Extension-shaped name without live metadata: safe defaults, redact all args."""
    return ResolvedToolRuntimeMetadata(
        stable_name=name,
        retrieval_aliases=(),
        result_limit=DEFAULT_TOOL_RESULT_LIMIT,
        timeout_sec=_DEFAULT_TIMEOUT_SEC,
        parallel_safe=False,
        log_redacted_args=None,
    )


def _default_metadata(name: str, *, timeout_sec: int = _DEFAULT_TIMEOUT_SEC) -> ResolvedToolRuntimeMetadata:
    return ResolvedToolRuntimeMetadata(
        stable_name=name,
        retrieval_aliases=(),
        result_limit=DEFAULT_TOOL_RESULT_LIMIT,
        timeout_sec=timeout_sec,
        parallel_safe=False,
        log_redacted_args=frozenset(),
    )


def _from_descriptor(name: str, descriptor: ToolDescriptor) -> ResolvedToolRuntimeMetadata:
    """The ordinary path: what the owning provider already stated, frozen as of now."""
    raw_limit = descriptor.result_limit
    result_limit = raw_limit if isinstance(raw_limit, int) and raw_limit > 0 else DEFAULT_TOOL_RESULT_LIMIT
    return ResolvedToolRuntimeMetadata(
        stable_name=descriptor.stable_name or name,
        retrieval_aliases=tuple(descriptor.retrieval_aliases),
        result_limit=min(result_limit, RESULT_LIMIT_HARD_MAX),
        timeout_sec=int(descriptor.timeout_sec),
        parallel_safe=bool(descriptor.parallel_safe),
        log_redacted_args=frozenset(descriptor.log_redacted_args),
        capability_class=str(descriptor.capability_class),
    )


def resolve_tool_runtime_metadata(
    name: str,
    catalog: ToolCatalog | None = None,
) -> ResolvedToolRuntimeMetadata:
    """Resolve one frozen runtime-metadata snapshot for the named tool.

    ``catalog`` is the ordered provider list. Without it only the static policy table and
    the fail-closed shapes are reachable — which is exactly what a caller outside a task
    has to work with, and why it is optional rather than required.

    Never raises: any internal failure returns the fail-closed shape for extension-shaped
    names, else plain defaults.
    """
    try:
        clean = str(name or "").strip()
    except Exception:
        clean = ""
    try:
        if catalog is not None:
            descriptor = catalog.find(clean)
            if descriptor is not None:
                return _from_descriptor(clean, descriptor)
        spec = TOOL_SPECS.get(clean)
        if spec is not None:
            # No provider offers the name, but policy still describes it — the retired
            # self-review tools are exactly this shape.
            return ResolvedToolRuntimeMetadata(
                stable_name=clean,
                retrieval_aliases=(),
                result_limit=spec.result_limit or DEFAULT_TOOL_RESULT_LIMIT,
                timeout_sec=int(spec.timeout_sec),
                parallel_safe=bool(spec.parallel),
                log_redacted_args=frozenset(),
                capability_class=str(spec.capability_class),
            )
        if _extension_shaped_tool_name(clean):
            return _fail_closed_metadata(clean)
        return _default_metadata(clean)
    except Exception:
        log.debug("tool runtime metadata resolution failed for %r", clean, exc_info=True)
        if _extension_shaped_tool_name(clean):
            return _fail_closed_metadata(clean)
        return _default_metadata(clean)
