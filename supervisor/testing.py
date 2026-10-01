"""Test helpers for supervisor runtime wiring."""

from __future__ import annotations

import pathlib
from typing import Any

from supervisor.bootstrap import build_supervisor_runtime
from supervisor.runtime_context import SupervisorRuntime, set_runtime


def bootstrap_supervisor_for_tests(
    tmp_path: pathlib.Path,
    *,
    soft_timeout: int = 10,
    hard_timeout: int = 20,
    bridge: Any = None,
) -> SupervisorRuntime:
    runtime = build_supervisor_runtime(
        settings={
            "OUROBOROS_SOFT_TIMEOUT_SEC": soft_timeout,
            "OUROBOROS_HARD_TIMEOUT_SEC": hard_timeout,
        },
        drive_root=pathlib.Path(tmp_path),
        repo_dir=pathlib.Path(tmp_path),
        bridge=bridge,
    )
    set_runtime(runtime)
    return runtime


__all__ = ["bootstrap_supervisor_for_tests"]
