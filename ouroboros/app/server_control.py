"""Process-control helpers for the self-editable server entrypoint."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any

_APP_ROOT = pathlib.Path(__file__).resolve().parents[2]


def restart_current_process(host: str, port: int, *, repo_dir: pathlib.Path, log: Any) -> None:
    env = os.environ.copy()
    desired_host = str(host)
    try:
        from ouroboros.app.config import load_settings

        desired_host = str(load_settings().OUROBOROS_SERVER_HOST or "").strip() or desired_host
    except Exception:
        desired_host = str(host)
    env["OUROBOROS_SERVER_HOST"] = desired_host
    env["OUROBOROS_SERVER_PORT"] = str(port)
    inherited_python_path = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_APP_ROOT), *(entry for entry in inherited_python_path if entry != str(_APP_ROOT))]
    )
    argv = [sys.executable, "-m", "ouroboros.app", *sys.argv[1:]]
    log.info("Re-executing direct server mode on %s:%d", desired_host, port)
    try:
        os.execvpe(sys.executable, argv, env)
    except Exception:
        log.exception("Direct re-exec failed; attempting spawned restart fallback.")
        try:
            subprocess.Popen(argv, env=env, cwd=str(repo_dir))
            log.info("Spawned replacement server process after exec failure.")
        except Exception:
            log.exception("Spawned restart fallback failed; exiting with restart code only.")
