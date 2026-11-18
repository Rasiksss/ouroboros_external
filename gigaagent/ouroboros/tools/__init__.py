"""Ouroboros tool package.

The tool declarations moved to ``new_core.tools``; this package now holds the tool
modules themselves plus dispatch and the prompt-surface adapter.

``from ouroboros.tools import ToolEntry`` was the other way a skill's ``tool.py`` could reach
the declaration types, so it is kept working the same way ``ouroboros.tools.registry`` is:
resolved lazily, reported on every use, handing back the ``new_core.tools`` object. See
``_deprecated`` for what is said about which name.

Unknown names raise a plain ``AttributeError`` — deliberately, because that is what makes the
import system fall through to loading a submodule for ``from ouroboros.tools import
skill_exec``.
"""

from __future__ import annotations

import sys
from typing import Any

from ouroboros.tools import _deprecated


def __getattr__(name: str) -> Any:
    # One frame up is the module that reached for the old path; see `registry.__getattr__`.
    return _deprecated.resolve(__name__, name, sys._getframe(1))
