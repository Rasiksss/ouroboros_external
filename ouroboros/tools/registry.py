"""Deprecated compatibility shim for the retired tool registry.

The registry itself is gone — its storage became the provider list under ``new_core/tools``,
its dispatch became ``ouroboros/tools/dispatch.py``, and its per-task context is now an
argument rather than a field. What survives here is only the *import path*, because a
``type: tool`` skill's ``tool.py`` on someone's drive may still be written against it:

    from ouroboros.tools.registry import ToolEntry, ToolSchema

Without this module that import raises, ``SkillToolProvider.refresh`` catches the error per
skill, and the skill's tools simply never appear — no message to the agent, no message to
the operator. A shim that warns is louder than an exception that gets swallowed.

The names are resolved lazily through the module ``__getattr__`` rather than bound at import
time. That is deliberate: ``__getattr__`` fires on **every** ``from ... import ...``, so each
skill that still uses the old path is reported, not just whichever one happened to import
first into a cached ``sys.modules`` entry.

The same treatment for ``from ouroboros.tools import ToolEntry`` lives in this package's
``__init__``; what both say about a given name is decided once, in ``_deprecated``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from ouroboros.tools import _deprecated

if TYPE_CHECKING:
    # Type-checking only: binding these at runtime would put them in the module dict, and
    # `__getattr__` — the whole point of this module — would never be reached.
    from new_core.tools.context import BrowserState, ToolContext
    from new_core.tools.entry import ToolEntry
    from new_core.tools.schema import ToolSchema

__all__ = ["BrowserState", "ToolContext", "ToolEntry", "ToolSchema"]


def __getattr__(name: str) -> Any:
    # `from x import y` resolves `y` straight from the importing frame, so one frame up is
    # the module that reached for the old path.
    return _deprecated.resolve(__name__, name, sys._getframe(1))


def __dir__() -> list[str]:
    return sorted(_deprecated.MOVED)
