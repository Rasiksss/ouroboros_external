"""The two paths background memory work needs, without the agent's ``Env``."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryEnv:
    """Where the repository is and where runtime data lives.

    The agent's ``Env`` lives in the package this one must not import.
    """

    repo_dir: pathlib.Path
    drive_root: pathlib.Path


__all__ = ["MemoryEnv"]
