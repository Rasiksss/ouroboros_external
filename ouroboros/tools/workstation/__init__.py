"""The five WorkStation-backed model tools (spec §19) plus ``workstation_connect``.

``workstation_connect`` is the explicit entry point: it creates or resumes
the sandbox session for this conversation.  The five file/exec tools are
gated on ``ctx.workstation`` and return ``TOOL_UNAVAILABLE`` until
``workstation_connect`` has been called.

Resolved by ``BuiltinToolProvider.refresh()`` via ``importlib.import_module``
(``new_core/tools/providers/builtin.py::FROZEN_TOOL_MODULES``), same as any
other built-in tools module (e.g. ``ouroboros/tools/gigasearch/__init__.py``).
"""

from __future__ import annotations

from new_core.tools.entry import ToolEntry
from ouroboros.tools.workstation import (
    apply_patch_tool,
    connect_tool,
    edit_tool,
    exec_tool,
    read_tool,
    write_tool,
)


def get_tools() -> list[ToolEntry]:
    return [
        *connect_tool.get_tools(),
        *exec_tool.get_tools(),
        *read_tool.get_tools(),
        *write_tool.get_tools(),
        *edit_tool.get_tools(),
        *apply_patch_tool.get_tools(),
    ]


__all__ = ["get_tools"]
