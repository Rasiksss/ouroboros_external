"""Shared machinery behind the two deprecated tool import paths.

`ouroboros.tools.registry` and the `ouroboros.tools` package itself both used to export the
tool declaration types. Both now resolve them lazily out of `new_core.tools` and report the
old path; this module is the one copy of "report it, then hand back the new object", so the
two shims cannot drift into saying different things about the same move.

Nothing here imports `new_core` at module level: the package `__init__` uses it, and
`new_core.tools` imports back into `ouroboros.tools` for the policy tables.
"""

from __future__ import annotations

import logging
import warnings
from types import FrameType
from typing import Any

#: Old name → the module that owns it now. Only the types a skill could legitimately have
#: imported; the registry class and the modules' private helpers have no replacement.
MOVED: dict[str, str] = {
    "BrowserState": "new_core.tools.context",
    "ToolContext": "new_core.tools.context",
    "ToolEntry": "new_core.tools.entry",
    "ToolSchema": "new_core.tools.schema",
}

#: Names worth explaining rather than denying flatly: they existed on the old path and a
#: reader deserves to know they were removed, not moved. Anything else gets the ordinary
#: `AttributeError` — which is also what lets `from ouroboros.tools import skill_exec` keep
#: falling through to the submodule import.
REMOVED: dict[str, str] = {
    "ToolRegistry": (
        "its storage became the provider list in `new_core.tools`, its dispatch became "
        "`ouroboros.tools.dispatch`, and its per-task context is now an argument — "
        "removed, not moved"
    ),
}

_HINT = "import it from `new_core.tools` instead; see docs/CREATING_SKILLS.md"

#: `from <package> import <name>` reaches a package's `__getattr__` twice: once as the import
#: machinery's `hasattr` probe (which is how `from ouroboros.tools import skill_exec` decides
#: whether to load a submodule), then once from the importing frame itself. Reporting the
#: probe would double every package-path warning and blame `<frozen importlib._bootstrap>`
#: for it. Plain modules like `registry` are never probed, so they see only the real access.
_IMPORT_MACHINERY = frozenset({"<frozen importlib._bootstrap>", "<frozen importlib._bootstrap_external>"})


def importer(frame: FrameType | None) -> str:
    """Format the caller's location for a log line that names the culprit.

    Best-effort by design: a missing or exotic frame must not turn a deprecation into a
    failure, so the caller passes whatever `sys._getframe` gave it, `None` included.
    """
    if frame is None:
        return "<unknown>"
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


def resolve(module_name: str, name: str, frame: FrameType | None) -> Any:
    """Report one use of a deprecated path, then return the object it moved to.

    `frame` is the *importer's* frame, taken by the shim itself — each shim is one call away
    from its caller, so neither has to know how deep this helper sits.
    """
    target = MOVED.get(name)
    if target is None:
        explanation = REMOVED.get(name)
        if explanation is not None:
            raise AttributeError(f"`{module_name}.{name}` is gone: {explanation}.")
        raise AttributeError(f"module {module_name!r} has no attribute {name!r}")

    module = __import__(target, fromlist=[name])
    resolved = getattr(module, name)
    if frame is not None and frame.f_code.co_filename in _IMPORT_MACHINERY:
        # The probe, not the use. The importing frame's own access follows immediately and
        # is what gets reported.
        return resolved

    message = f"`{module_name}.{name}` is deprecated and will be removed: {_HINT}"
    # stacklevel 3: this frame, the shim's `__getattr__`, then the importing module.
    warnings.warn(message, DeprecationWarning, stacklevel=3)
    logging.getLogger(module_name).warning("%s (imported by %s)", message, importer(frame))
    return resolved
