"""Subprocess tracking helpers used by panic shutdown."""

from __future__ import annotations

import subprocess
import threading
from subprocess import Popen

from ouroboros.platform_layer import kill_process_tree, subprocess_new_group_kwargs

_active_subprocesses: set = set()
_subprocess_lock = threading.Lock()


def _kill_process_group(proc) -> None:
    """Kill a subprocess tree (shared with skill_exec panic tracking)."""
    kill_process_tree(proc)


def tracked_subprocess_run(cmd, **kwargs):
    """subprocess.run replacement with process-tree tracking."""
    from subprocess import CompletedProcess

    timeout = kwargs.pop("timeout", None)
    kwargs.update(subprocess_new_group_kwargs())
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = Popen(cmd, **kwargs)
    with _subprocess_lock:
        _active_subprocesses.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait(timeout=5)
        raise
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)


def kill_all_tracked_subprocesses() -> None:
    """Kill all tracked subprocess trees on panic."""
    with _subprocess_lock:
        procs = list(_active_subprocesses)
    for proc in procs:
        _kill_process_group(proc)
    with _subprocess_lock:
        _active_subprocesses.clear()
