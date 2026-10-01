"""The ordered provider list, as one object.

Every tool source is an ``IToolProvider``; the catalog is the list of them in resolution
order, and on a name collision the first provider wins (D5). Consumers take a
``ToolCatalog`` rather than a bare sequence plus a handful of free functions.

There is **one catalog per process**: the module-level ``tool_catalog``, constructed empty
at import and wired by the server lifespan through ``initialize()``. Construction and
wiring are separate because the providers need roots and a loaded runtime that only exist
once the app is starting, while importers need the object at import time. The catalog was
previously rebuilt per task (``ToolCatalog.standard``) and once more for the A2A card;
those three catalogs re-walked the same filesystem and could disagree with each other.

It deliberately does **not** hold a ``ToolContext``. That is the line between this and the
retired ``ToolRegistry``: the registry combined storage, dispatch, and a per-task context,
and 17 call sites reached through it for the context alone. Here the context stays an
argument on ``execute`` (D3), so nothing about a task outlives the call — which is what
lets one catalog serve every task, concurrent ones included.

Besides *which* provider owns a name, the catalog also resolves *where* the call runs: it
holds the descriptor and the provider together, so ``descriptor.runtime or
provider.default_runtime`` is decided here and by nothing else. Execution has one entry
point, the asynchronous ``aexecute``. It briefly had a synchronous twin as well, for the
callers still above it; those are coroutines now, and the crossing moved up to
``handle_tool_calls`` — once per round rather than once per tool call. Two bridges would be
two answers to the same question.

Not to be confused with ``ouroboros.tools.catalog``, which is the static policy table
(``TOOL_SPECS``) describing what each tool is allowed to do.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import IToolProvider, ToolDescriptor

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)

__all__ = ["ToolCatalog", "tool_catalog"]


class ToolCatalog:
    """Catalog reads, execution, and lifecycle across an ordered list of providers."""

    def __init__(self) -> None:
        self._providers: list[IToolProvider] = []

    def initialize(self, providers: Sequence[IToolProvider]) -> None:
        """Install the provider list and refresh it — the single wiring point.

        Replaces rather than appends, so a second call (a re-entered lifespan, a test
        rewiring the shared catalog) installs exactly the list it was given instead of
        stacking a duplicate set behind the first, where every name would be shadowed.
        """
        self._providers = list(providers)
        self.refresh()

    # --- composition ---------------------------------------------------------------

    @property
    def providers(self) -> Sequence[IToolProvider]:
        """The providers in resolution order. Read-only; use ``add`` to extend."""
        return tuple(self._providers)

    def add(self, provider: IToolProvider) -> None:
        """Append a provider at the end, where it loses every existing name collision."""
        self._providers.append(provider)

    def __iter__(self) -> Iterator[IToolProvider]:
        return iter(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    # --- lifecycle -----------------------------------------------------------------

    def refresh(self) -> None:
        """Refresh every provider in order, reporting shadowed names once."""
        for provider in self._providers:
            provider.refresh()
        seen: dict[str, str] = {}
        for provider in self._providers:
            for desc in provider.descriptors():
                owner = seen.get(desc.name)
                if owner is None:
                    seen[desc.name] = desc.origin
                elif owner != desc.origin:
                    log.warning(
                        "Tool name collision: %s from %s is shadowed by %s; keeping the first",
                        desc.name,
                        desc.origin,
                        owner,
                    )

    def close(self) -> None:
        for provider in self._providers:
            try:
                provider.close()
            except Exception:
                log.debug("Provider %s failed to close", type(provider).__name__, exc_info=True)

    # --- reads ---------------------------------------------------------------------

    def descriptors(self) -> list[ToolDescriptor]:
        """Every descriptor across the list; on a name collision the first wins."""
        result: list[ToolDescriptor] = []
        seen: set[str] = set()
        for provider in self._providers:
            for desc in provider.descriptors():
                if desc.name in seen:
                    continue
                seen.add(desc.name)
                result.append(desc)
        return result

    def find(self, name: str) -> ToolDescriptor | None:
        """Resolve a name — including provider-local aliases — against the list in order."""
        owner = self._owner(name)
        return owner[1] if owner is not None else None

    def refuse_dispatch(self, name: str) -> str | None:
        """The first provider's refusal for a name it owns but withholds — or ``None``.

        Asked only when ``find`` came back empty, so that a name no provider will *offer*
        is still explained by whichever provider owns it. Same order as every other read:
        the first provider with something to say wins.
        """
        requested = str(name or "").strip()
        if not requested:
            return None
        for provider in self._providers:
            refusal = provider.refuse_dispatch(requested)
            if refusal is not None:
                return refusal
        return None

    def _owner(self, name: str) -> tuple[IToolProvider, ToolDescriptor] | None:
        requested = str(name or "").strip()
        if not requested:
            return None
        for provider in self._providers:
            desc = provider.get(requested)
            if desc is not None:
                return provider, desc
        return None

    # --- local-provider reads ------------------------------------------------------
    #
    # Only builtin and skill providers hold a local callable, and only they expose
    # ``entries()``. These reads go through it rather than ``descriptors()`` because the
    # extension provider filters its descriptors by liveness, and each check walks the
    # filesystem — far too costly for lookups that run on every tool call.

    def local_entries(self) -> dict[str, ToolEntry]:
        """The entries the local providers hold, first-wins across the list."""
        merged: dict[str, ToolEntry] = {}
        for provider in self._providers:
            entries = getattr(provider, "entries", None)
            if not callable(entries):
                continue
            for name, entry in entries().items():
                merged.setdefault(name, entry)
        return merged

    def local_tool_names(self) -> list[str]:
        """Sorted names of the repo and skill tools."""
        return sorted(self.local_entries())

    def code_tool_names(self) -> frozenset[str]:
        """Tools that run code. Extension and MCP descriptors never set the flag."""
        return frozenset(name for name, entry in self.local_entries().items() if entry.is_code_tool)

    # --- execution -----------------------------------------------------------------

    async def aexecute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        """Delegate to the owning provider, through the runtime that places the call.

        Gates run before this. The catalog is the only place holding both halves of the
        answer — ``_owner`` returns the provider and the descriptor together — so the
        descriptor's own runtime winning over the provider's default is resolved here and
        nowhere else. Callers reaching a descriptor through ``find()`` have no provider and
        must not attempt it themselves.
        """
        owner = self._owner(name)
        if owner is None:
            return f"⚠️ Unknown tool: {name}"
        provider, descriptor = owner
        runtime = descriptor.runtime or provider.default_runtime
        return await runtime.run(provider, ctx, descriptor.name, args)


#: The process-wide catalog. Empty until the server lifespan calls ``initialize()``;
#: import the object, never a snapshot of its contents.
tool_catalog = ToolCatalog()
