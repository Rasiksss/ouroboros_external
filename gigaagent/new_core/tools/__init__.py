"""Tool declarations and the provider implementations behind them.

**Exempt from the ``new_core`` import ban.** ``graph`` and ``loop`` stay free of
``ouroboros`` imports and are enforced by their own boundary tests; this package is a
deliberate exception, because the providers reach the skill, extension, and MCP machinery
directly and ``ToolContext`` carries ouroboros paths and policy types.

This is the import path for skill-authored tools: ``from new_core.tools import ToolEntry``.
The former ``ouroboros.tools.registry`` was deleted rather than kept as a re-export, so a
``tool.py`` still written against it fails to import — see ``docs/CREATING_SKILLS.md``.
"""

from __future__ import annotations

from new_core.tools.catalog import ToolCatalog, tool_catalog
from new_core.tools.context import BrowserState, ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import (
    IToolProvider,
    NullToolEvents,
    ToolDescriptor,
    ToolEvents,
    ToolSurface,
)
from new_core.tools.schema import ToolSchema

__all__ = [
    "BrowserState",
    "IToolProvider",
    "NullToolEvents",
    "ToolCatalog",
    "ToolContext",
    "ToolDescriptor",
    "ToolEntry",
    "ToolEvents",
    "ToolSchema",
    "ToolSurface",
    "tool_catalog",
]
