"""The five tool sources, each behind ``IToolProvider``.

Resolution order inside ``ToolCatalog`` (D5) — first match wins:

    BuiltinToolProvider -> SkillToolProvider -> ExtensionToolProvider -> McpToolProvider
    -> A2aToolProvider
"""

from __future__ import annotations

from new_core.tools.providers.a2a import A2aToolProvider
from new_core.tools.providers.builtin import BuiltinToolProvider
from new_core.tools.providers.extension import ExtensionToolProvider
from new_core.tools.providers.mcp import McpToolProvider
from new_core.tools.providers.skill import SkillToolProvider

__all__ = [
    "A2aToolProvider",
    "BuiltinToolProvider",
    "ExtensionToolProvider",
    "McpToolProvider",
    "SkillToolProvider",
]
