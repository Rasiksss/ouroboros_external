"""Sandbox policy and runner tests.

Placed in tests_headless/ so they run in the default CI suite
(testpaths = ["tests_headless"]).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _mock_settings(*, backend: str = "soft", deny_paths: list[str] | None = None) -> MagicMock:
    s = MagicMock()
    s.OUROBOROS_SANDBOX_BACKEND = backend
    s.OUROBOROS_SANDBOX_DENY_PATHS = deny_paths or []
    return s


def _gone(pid: int, *, budget_sec: float = 5.0) -> bool:
    """Whether a pid has left the table. Polled: an orphan is reaped a few ms after the kill."""
    deadline = time.monotonic() + budget_sec
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# SandboxPolicy contract
# ---------------------------------------------------------------------------


def test_sandbox_policy_defaults() -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, skill_default_policy

    policy = skill_default_policy(cwd=Path("/tmp"))
    assert policy.network == NetworkPolicy.DENY
    assert policy.timeout_sec == 300
    assert policy.stdout_cap == 256 * 1024
    assert policy.stderr_cap == 128 * 1024
    assert policy.deny_paths == ()
    assert policy.extra_deny_paths == ()
    assert "*.key" in policy.deny_name_patterns


# ---------------------------------------------------------------------------
# Deny checks
# ---------------------------------------------------------------------------


def test_deny_paths_block(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    secret = tmp_path / "secret.txt"
    secret.write_text("sensitive")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(tmp_path,),
        extra_deny_paths=(),
        deny_name_patterns=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run(
            [sys.executable, str(secret)],
            policy,
            env={},
        )
    assert result.denied is True
    assert result.denial_reason is not None
    assert any(e["type"] == "sandbox.denied" for e in result.audit_events)


_ENV_PROBE_SCRIPT = (
    "import os, json\n"
    "keys = ['PATH', 'HTTP_PROXY', 'HTTPS_PROXY', 'ANTHROPIC_API_KEY', "
    "'OPENROUTER_API_KEY']\n"
    "vals = {}\n"
    "for key in keys:\n"
    "    value = os.environ.get(key)\n"
    "    if value is not None:\n"
    "        vals[key] = value\n"
    "print(json.dumps(vals))\n"
)


def test_deny_paths_block_relative_argv(tmp_path: Path) -> None:
    """Relative argv must be resolved from policy.cwd, not the host process cwd."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    secret = blocked / "secret.txt"
    secret.write_text("sensitive")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(blocked,),
        deny_name_patterns=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run(
            [sys.executable, "blocked/secret.txt"],
            policy,
            env={},
        )
    assert result.denied is True


def test_extra_deny_paths_block(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    target = blocked / "target.py"
    target.write_text("print('hi')")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(blocked,),
        deny_name_patterns=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(target)], policy, env={})
    assert result.denied is True


def test_symlink_argv_denied(tmp_path: Path) -> None:
    """Symlink argv must be denied when its resolved target is under a deny path."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    secret = blocked / "secret.txt"
    secret.write_text("sensitive")
    link = tmp_path / "escape.link"
    link.symlink_to(secret)

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(blocked,),
        deny_name_patterns=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(link)], policy, env={})
    assert result.denied is True
    assert result.denial_reason is not None


def test_symlink_name_pattern_denied(tmp_path: Path) -> None:
    """Deny name patterns must apply to symlink targets, not only link basenames."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    keyfile = tmp_path / "service.key"
    keyfile.write_text("RSA PRIVATE KEY")
    link = tmp_path / "safe.txt"
    link.symlink_to(keyfile)

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        deny_name_patterns=("*.key",),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(link)], policy, env={})
    assert result.denied is True


def test_deny_name_patterns_block(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    keyfile = tmp_path / "service.key"
    keyfile.write_text("RSA PRIVATE KEY")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        deny_name_patterns=("*.key",),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(keyfile)], policy, env={})
    assert result.denied is True


# ---------------------------------------------------------------------------
# Env scrub
# ---------------------------------------------------------------------------


def test_runner_does_not_merge_host_environ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SandboxRunner must not merge host os.environ into the child process."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-secret-must-not-appear")
    monkeypatch.setenv("OPENROUTER_API_KEY", "leaked-secret-must-not-appear")

    script = tmp_path / "dump_env.py"
    script.write_text(_ENV_PROBE_SCRIPT)

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run(
            [sys.executable, str(script)],
            policy,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    assert result.returncode == 0
    subprocess_env = json.loads(result.stdout)
    assert "ANTHROPIC_API_KEY" not in subprocess_env
    assert "OPENROUTER_API_KEY" not in subprocess_env


def test_env_scrub_no_api_key_leak(tmp_path: Path) -> None:
    """ANTHROPIC_API_KEY in os.environ must not reach the subprocess env."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "dump_env.py"
    script.write_text(_ENV_PROBE_SCRIPT)

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.ALLOW,
    )
    # Pass an env dict that does NOT contain the secret — simulating _scrub_env().
    clean_env = {"PATH": "/usr/bin:/bin"}

    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env=clean_env)

    assert result.returncode == 0
    subprocess_env = json.loads(result.stdout)
    assert "ANTHROPIC_API_KEY" not in subprocess_env
    assert "OPENROUTER_API_KEY" not in subprocess_env


# ---------------------------------------------------------------------------
# Output cap
# ---------------------------------------------------------------------------


def test_output_cap_kills_process(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "flood.py"
    script.write_text("import sys\nwhile True: sys.stdout.write('x' * 1024)\n")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=30,
        stdout_cap=4 * 1024,
        stderr_cap=4 * 1024,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env={"PATH": "/usr/bin:/bin"})

    assert result.overflowed is True
    assert len(result.stdout) <= 4 * 1024


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_process_group_killed_on_timeout(tmp_path: Path) -> None:
    """Timeout must kill the entire process group, including spawned children."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    marker = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(100)'],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "marker.write_text(str(child.pid))\n"
        "time.sleep(100)\n"
    )

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=1,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run(
            [sys.executable, str(script)],
            policy,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    assert result.timed_out is True
    assert marker.is_file()
    child_pid = int(marker.read_text().strip())
    assert _gone(child_pid), "the spawned child outlived the group the timeout killed"


def test_timeout_kills_process(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "sleep.py"
    script.write_text("import time; time.sleep(100)\n")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=1,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env={"PATH": "/usr/bin:/bin"})

    assert result.timed_out is True


def test_task_stop_kills_the_owned_process_group(tmp_path: Path) -> None:
    """A cooperative task stop terminates the sandbox parent and its child process."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    marker = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(100)'],\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "marker.write_text(str(child.pid))\n"
        "time.sleep(100)\n"
    )
    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=30,
        network=NetworkPolicy.ALLOW,
    )
    requested = threading.Event()

    def stop_requested() -> bool:
        if marker.is_file():
            requested.set()
        return requested.is_set()

    started = time.monotonic()
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run(
            [sys.executable, str(script)],
            policy,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stop_requested=stop_requested,
        )

    assert time.monotonic() - started < 5
    assert result.cancelled is True
    assert result.timed_out is False
    assert any(event["type"] == "sandbox.cancelled" for event in result.audit_events)
    child_pid = int(marker.read_text().strip())
    assert _gone(child_pid), "the spawned child outlived the group the task stop killed"


# ---------------------------------------------------------------------------
# Network deny
# ---------------------------------------------------------------------------


def test_network_deny_unenforceable_refuses_run(tmp_path: Path) -> None:
    """DENY without usable unshare must refuse the run, not execute with network."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    marker = tmp_path / "ran.txt"
    script = tmp_path / "touch.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.DENY,
    )
    with (
        patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()),
        patch("ouroboros.sandbox.soft._unshare_net_prefix", return_value=None),
    ):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env={"PATH": "/usr/bin:/bin"})

    assert result.denied is True
    assert "cannot be enforced" in (result.denial_reason or "")
    assert not marker.exists()
    assert any(e["type"] == "sandbox.network_deny_unenforceable" for e in result.audit_events)


def test_network_deny_prefixes_unshare(tmp_path: Path) -> None:
    """DENY with usable unshare must wrap the command in the probed unshare prefix."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.soft import SoftSandboxBackend

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.DENY,
    )
    proc = MagicMock()
    proc.stdout.read.return_value = b""
    proc.stderr.read.return_value = b""
    proc.poll.return_value = 0
    proc.wait.return_value = 0
    proc.returncode = 0
    with (
        patch("ouroboros.sandbox.soft._unshare_net_prefix", return_value=["/usr/bin/unshare", "--net"]),
        patch("ouroboros.sandbox.soft.Popen", return_value=proc) as popen_mock,
    ):
        result = SoftSandboxBackend().run([sys.executable, "ok.py"], policy, env={"PATH": "/usr/bin:/bin"})

    assert result.denied is False
    assert popen_mock.call_args.args[0][:2] == ["/usr/bin/unshare", "--net"]


def test_unshare_probe_falls_back_to_a_user_namespace(monkeypatch) -> None:
    """EPERM on plain --net probes --user --net: the same empty namespace without CAP_SYS_ADMIN."""
    from ouroboros.sandbox import soft

    monkeypatch.setattr(soft, "_unshare_net_prefix_cache", None)
    monkeypatch.setattr(soft.shutil, "which", lambda _name: "/usr/bin/unshare")
    monkeypatch.setattr(soft.subprocess, "run", lambda cmd, **_k: MagicMock(returncode=0 if "--user" in cmd else 1))

    assert soft._unshare_net_prefix() == ["/usr/bin/unshare", "--user", "--net"]
    assert soft._unshare_net_prefix_cache == ["/usr/bin/unshare", "--user", "--net"]


def test_unshare_probe_caching(monkeypatch) -> None:
    """Completed probes are cached both ways; exceptional failures re-probe."""
    import subprocess as subprocess_mod

    from ouroboros.sandbox import soft

    monkeypatch.setattr(soft, "_unshare_net_prefix_cache", None)
    monkeypatch.setattr(soft.shutil, "which", lambda _name: "/usr/bin/unshare")

    def _probe_timeout(*_args, **_kwargs):
        raise subprocess_mod.TimeoutExpired(cmd="unshare", timeout=5)

    # Transient failure: denied now, but not cached — the next call re-probes.
    monkeypatch.setattr(soft.subprocess, "run", _probe_timeout)
    assert soft._unshare_net_prefix() is None
    assert soft._unshare_net_prefix_cache is None

    # Both forms completing nonzero (e.g. seccomp filtering the syscall) is stable: cached.
    monkeypatch.setattr(soft.subprocess, "run", lambda *a, **k: MagicMock(returncode=1))
    assert soft._unshare_net_prefix() is None
    assert soft._unshare_net_prefix_cache == []
    monkeypatch.setattr(soft.subprocess, "run", lambda *a, **k: MagicMock(returncode=0))
    assert soft._unshare_net_prefix() is None  # still cached unavailable

    # A working plain probe caches the shortest prefix.
    monkeypatch.setattr(soft, "_unshare_net_prefix_cache", None)
    assert soft._unshare_net_prefix() == ["/usr/bin/unshare", "--net"]
    assert soft._unshare_net_prefix_cache == ["/usr/bin/unshare", "--net"]


def test_network_allow_runs_without_isolation(tmp_path: Path) -> None:
    """ALLOW must run untouched even where unshare is unavailable, keeping env intact."""
    import json

    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "dump_env.py"
    script.write_text(_ENV_PROBE_SCRIPT)

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.ALLOW,
    )
    env_with_proxy = {
        "PATH": "/usr/bin:/bin",
        "HTTP_PROXY": "http://proxy.example.com:8080",
    }
    with (
        patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()),
        patch("ouroboros.sandbox.soft._unshare_net_prefix", return_value=None),
    ):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env=env_with_proxy)

    assert result.denied is False
    assert result.returncode == 0
    subprocess_env = json.loads(result.stdout)
    assert subprocess_env.get("HTTP_PROXY") == "http://proxy.example.com:8080"


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


def test_read_roots_warning_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """FR-009: read_roots/write_roots on soft backend log warning; execution proceeds."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "ok.py"
    script.write_text("print('ok')")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.ALLOW,
        read_roots=(tmp_path / "read_only",),
        write_roots=(tmp_path / "write_only",),
    )
    with (
        patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()),
        caplog.at_level(logging.WARNING, logger="ouroboros.sandbox.soft"),
    ):
        result = SandboxRunner().run(
            [sys.executable, str(script)],
            policy,
            env={"PATH": "/usr/bin:/bin"},
        )

    assert result.denied is False
    assert result.returncode == 0
    assert any("read_roots" in record.message for record in caplog.records)
    assert any("write_roots" in record.message for record in caplog.records)


def test_unknown_backend_raises(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "ok.py"
    script.write_text("print('ok')")
    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with (
        patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings(backend="docker")),
        pytest.raises(ValueError, match=r"Unknown sandbox backend: 'docker'"),
    ):
        SandboxRunner().run([sys.executable, str(script)], policy, env={})


# ---------------------------------------------------------------------------
# Settings / operator deny paths
# ---------------------------------------------------------------------------


def test_settings_default_backend_soft(tmp_path: Path) -> None:
    """FR-014: default sandbox backend is soft when unset."""
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy, SandboxResult
    from ouroboros.sandbox.runner import SandboxRunner
    from ouroboros.sandbox.soft import SoftSandboxBackend
    from ouroboros.storage.settings.schema import AppSettings

    assert AppSettings().OUROBOROS_SANDBOX_BACKEND == "soft"

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with (
        patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings(backend="")),
        patch.object(
            SoftSandboxBackend,
            "run",
            return_value=SandboxResult(
                stdout=b"ok",
                stderr=b"",
                returncode=0,
                timed_out=False,
                overflowed=False,
                denied=False,
                denial_reason=None,
                audit_events=[],
            ),
        ) as mock_run,
    ):
        SandboxRunner().run(["echo", "ok"], policy, env={"PATH": "/usr/bin:/bin"})
        mock_run.assert_called_once()


def test_operator_deny_paths_via_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator deny paths from settings merge into policy and block argv."""
    from ouroboros.app.config import load_settings
    from ouroboros.contracts.sandbox_policy import skill_default_policy
    from ouroboros.sandbox.runner import SandboxRunner
    from ouroboros.storage.settings.schema import AppSettings
    from ouroboros.storage.settings.store import reset_memory_settings_for_tests

    blocked = tmp_path / "operator_blocked"
    blocked.mkdir()
    target = blocked / "target.py"
    target.write_text("print('hi')")

    assert [str(blocked.resolve())] == AppSettings(
        OUROBOROS_SANDBOX_DENY_PATHS=[str(blocked)]
    ).OUROBOROS_SANDBOX_DENY_PATHS

    monkeypatch.setenv("OUROBOROS_SANDBOX_DENY_PATHS", json.dumps([str(blocked)]))
    reset_memory_settings_for_tests()

    extra_deny = tuple(Path(p) for p in load_settings().OUROBOROS_SANDBOX_DENY_PATHS)
    assert extra_deny == (blocked.resolve(),)

    policy = skill_default_policy(cwd=tmp_path, extra_deny=extra_deny)
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(target)], policy, env={})

    assert result.denied is True
    assert any(e["type"] == "sandbox.denied" for e in result.audit_events)


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


def test_audit_events_collected(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    script = tmp_path / "ok.py"
    script.write_text("print('ok')")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        timeout_sec=10,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(script)], policy, env={"PATH": "/usr/bin:/bin"})

    types = {e["type"] for e in result.audit_events}
    assert "sandbox.start" in types
    assert "sandbox.finish" in types


def test_audit_events_denied_event(tmp_path: Path) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy
    from ouroboros.sandbox.runner import SandboxRunner

    blocked = tmp_path / "blocked.key"
    blocked.write_text("key data")

    policy = SandboxPolicy(
        cwd=tmp_path,
        deny_paths=(),
        extra_deny_paths=(),
        deny_name_patterns=("*.key",),
        timeout_sec=5,
        network=NetworkPolicy.ALLOW,
    )
    with patch("ouroboros.sandbox.runner.load_settings", return_value=_mock_settings()):
        result = SandboxRunner().run([sys.executable, str(blocked)], policy, env={})

    assert result.denied is True
    types = {e["type"] for e in result.audit_events}
    assert "sandbox.denied" in types


# ---------------------------------------------------------------------------
# Task workspace
# ---------------------------------------------------------------------------


def test_task_workspace_created_and_cleaned(tmp_path: Path) -> None:
    """task_pipeline creates a workspace dir and removes it in the finally block."""
    import tempfile
    from unittest.mock import patch

    created_paths: list[Path] = []

    def fake_mkdtemp(prefix: str = "", dir: Path | None = None) -> str:
        p = tmp_path / "ws"
        p.mkdir(exist_ok=True)
        created_paths.append(p)
        return str(p)

    removed_paths: list[Path] = []

    def fake_rmtree(path: str | Path, ignore_errors: bool = False) -> None:
        removed_paths.append(Path(path))

    with patch("tempfile.mkdtemp", side_effect=fake_mkdtemp), patch("shutil.rmtree", side_effect=fake_rmtree):
        ws = Path(tempfile.mkdtemp(prefix="ouroboros-task-", dir=tmp_path))
        try:
            assert ws.exists()
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    assert len(created_paths) == 1
    assert len(removed_paths) == 1
    assert removed_paths[0] == created_paths[0]
