"""Run the package scripts the way skill_exec does: a fresh interpreter, a scrubbed env."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from collections.abc import Callable

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"

RunScript = Callable[..., tuple[int, dict]]


@pytest.fixture
def run_script() -> RunScript:
    def run(script: str, workspace: pathlib.Path | None, *args: str) -> tuple[int, dict]:
        env = {"PATH": "/usr/bin:/bin"}
        if workspace is not None:
            env["OUROBOROS_SKILL_WORKDIR"] = str(workspace)
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode, json.loads(result.stdout)

    return run
