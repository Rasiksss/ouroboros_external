"""The ``ToolSurface`` a running tool sees through its ``ToolContext``.

Reads the catalog off the ordered provider list, and records surface-change *intents* that
travel on the same override channel the model/effort/compaction overrides already use. The
node applies them at the top of the next round; nothing here mutates run state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from config_registry.user_settings.tool_access import skill_tool_refusal
from new_core.identity import OwnerScope
from new_core.tools.catalog import ToolCatalog
from new_core.tools.interfaces import ToolDescriptor, ToolSurface


class ProviderToolSurface(ToolSurface):
    """Catalog reads over the provider list, plus the pending-enable intent buffer."""

    def __init__(
        self,
        catalog: ToolCatalog,
        active_names: set[str],
        scope: OwnerScope,
        skill_origins: frozenset[str] | None = None,
    ) -> None:
        self._catalog = catalog
        # The live set the graph owns; read, never written here.
        self._active_names = active_names
        self._scope = scope
        self._skill_origins = skill_origins
        self._pending: list[tuple[str, Any]] = []

    def catalog(self) -> Sequence[ToolDescriptor]:
        return tuple(
            desc
            for desc in self._catalog.descriptors(self._scope)
            if skill_tool_refusal(desc.name, desc.origin, self._skill_origins) is None
        )

    def find(self, name: str) -> ToolDescriptor | None:
        desc = self._catalog.find(name, self._scope)
        return (
            desc
            if desc is not None and skill_tool_refusal(desc.name, desc.origin, self._skill_origins) is None
            else None
        )

    def active_names(self) -> frozenset[str]:
        """Applied names plus intents recorded earlier in this round.

        Without the union, two ``enable_tools`` calls in one round would stop seeing each
        other and the second would answer "Enabled: X" rather than "already active".
        """
        return frozenset(self._active_names) | {name for name, _ in self._pending}

    def request_enable(self, name: str, schema: Any) -> None:
        if name in self.active_names():
            return
        self._pending.append((name, schema))

    def drain_pending_enables(self) -> list[tuple[str, Any]]:
        """Hand the recorded intents to the override channel and reset the buffer."""
        pending = list(self._pending)
        self._pending.clear()
        return pending


__all__ = ["ProviderToolSurface"]
