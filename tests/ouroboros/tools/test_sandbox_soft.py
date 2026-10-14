"""SoftSandboxBackend — stop predicate exception safety."""

from __future__ import annotations

import sys

import pytest

from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
from ouroboros.sandbox import soft
from ouroboros.sandbox.soft import SoftSandboxBackend


def _policy(tmp_path, *, timeout_sec: int = 10) -> SandboxPolicy:
    return SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        deny_name_patterns=(),
        timeout_sec=timeout_sec,
        stdout_cap=4096,
        stderr_cap=4096,
        network=NetworkPolicy.ALLOW,
    )


def test_predicate_exception_kills_the_process(tmp_path, monkeypatch) -> None:
    """A raising stop predicate must kill the child, not orphan it."""
    backend = SoftSandboxBackend()
    policy = _policy(tmp_path, timeout_sec=30)
    script = "import time, sys\nsys.stdout.write('alive\\n')\nsys.stdout.flush()\ntime.sleep(20)\n"
    script_path = tmp_path / "long.py"
    script_path.write_text(script)
    killed_processes = []
    original_kill = soft._kill_process_group

    def _record_kill(proc) -> None:
        killed_processes.append(proc)
        original_kill(proc)

    monkeypatch.setattr(soft, "_kill_process_group", _record_kill)

    calls: list[int] = []

    def _flaky_predicate() -> bool:
        calls.append(1)
        if len(calls) >= 2:
            raise RuntimeError("DB unavailable")
        return False

    with pytest.raises(RuntimeError, match="DB unavailable"):
        backend.run(
            [sys.executable, str(script_path)],
            policy,
            env={"PATH": ""},
            stop_requested=_flaky_predicate,
        )

    assert len(killed_processes) == 1
    assert killed_processes[0].poll() is not None


def test_predicate_returning_true_cancels_and_kills(tmp_path) -> None:
    """Normal cancellation path still works after the fix."""
    backend = SoftSandboxBackend()
    policy = _policy(tmp_path, timeout_sec=30)
    script = "import time\nprint('alive')\ntime.sleep(20)\n"
    script_path = tmp_path / "long.py"
    script_path.write_text(script)

    result = backend.run(
        [sys.executable, str(script_path)],
        policy,
        env={"PATH": ""},
        stop_requested=lambda: True,
    )

    assert result.cancelled is True
    assert result.returncode != 0
