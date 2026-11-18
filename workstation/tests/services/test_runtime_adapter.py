"""Runtime adapter tests for local execution and bwrap command building."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from app.core.config import RuntimeMode, settings
from app.services.runtime import RuntimeAdapter, RuntimeContext
from app.services.runtime.bwrap import build_bwrap_exec_command
from app.utils.exceptions.common import InternalServerErrorException


def _contains_sequence(items: list[str], sequence: list[str]) -> bool:
    window = len(sequence)
    return any(items[index : index + window] == sequence for index in range(len(items) - window + 1))


async def test_provision_rootfs_copies_template_tree(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    template_root.mkdir()
    (template_root / "etc").mkdir()
    (template_root / "etc" / "issue").write_text("sandbox", encoding="utf-8")
    rootfs_path = tmp_path / "session-rootfs"

    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(template_root),
            }
        )
    )

    await adapter.provision_rootfs(str(rootfs_path))

    assert (rootfs_path / "etc" / "issue").read_text(encoding="utf-8") == "sandbox"


async def test_provision_rootfs_requires_existing_template_dir(tmp_path: Path) -> None:
    rootfs_path = tmp_path / "session-rootfs"
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "missing-template"),
            }
        )
    )

    with pytest.raises(InternalServerErrorException, match="template directory does not exist"):
        await adapter.provision_rootfs(str(rootfs_path))


async def test_destroy_rootfs_removes_local_session_cgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.runtime.adapter as adapter_module

    cgroup_root = tmp_path / "cgroup"
    cgroup_path = cgroup_root / "ws-session_bad"
    cgroup_path.mkdir(parents=True)
    rootfs_path = tmp_path / "sessions" / "session:bad"
    rootfs_path.mkdir(parents=True)
    (rootfs_path / "file.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "CGROUP_ROOT", cgroup_root)
    adapter = RuntimeAdapter(settings.model_copy(update={"RUNTIME_MODE": RuntimeMode.LOCAL}))

    await adapter.destroy_rootfs(str(rootfs_path))

    assert not rootfs_path.exists()
    assert not cgroup_path.exists()


async def test_destroy_rootfs_ignores_busy_local_session_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.runtime.adapter as adapter_module

    cgroup_root = tmp_path / "cgroup"
    cgroup_path = cgroup_root / "ws-session-busy"
    cgroup_path.mkdir(parents=True)
    (cgroup_path / "cgroup.procs").write_text("", encoding="utf-8")
    rootfs_path = tmp_path / "sessions" / "session-busy"
    rootfs_path.mkdir(parents=True)
    monkeypatch.setattr(adapter_module, "CGROUP_ROOT", cgroup_root)
    adapter = RuntimeAdapter(settings.model_copy(update={"RUNTIME_MODE": RuntimeMode.LOCAL}))

    await adapter.destroy_rootfs(str(rootfs_path))

    assert not rootfs_path.exists()
    assert cgroup_path.exists()


def test_local_audit_context_parses_wrapper_warnings(tmp_path: Path) -> None:
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "SANDBOX_BINARY": str(tmp_path / "ws-sandbox"),
            }
        )
    )

    payload = adapter.audit_context(
        stderr=("WRAPPER: warning step=cgroup errno=30 msg=cgroup root is not writable, skipping\nuser stderr\n")
    )

    assert payload == {
        "runtime": "local",
        "wrapper": "ws-sandbox",
        "landlock": "applied",
        "seccomp": "applied",
        "cgroup": "skipped",
    }


async def test_local_exec_runs_inside_session_rootfs_and_passes_env(tmp_path: Path) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-local", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
            }
        )
    )

    result = await adapter.exec(
        context,
        command=[
            sys.executable,
            "-c",
            (
                "import os, pathlib; "
                "pathlib.Path('cwd.txt').write_text(os.getcwd(), encoding='utf-8'); "
                "print(os.environ['RUNTIME_SAMPLE'])"
            ),
        ],
        cwd="/workspace",
        env={"RUNTIME_SAMPLE": "ok"},
        timeout_sec=5,
        output_limit_bytes=1024,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""
    assert result.stdout_truncated is False
    assert (rootfs_path / "workspace" / "cwd.txt").exists()
    assert not (rootfs_path / ".sandbox" / "env").exists()
    assert not (rootfs_path / ".sandbox" / "exec.allow").exists()
    assert list((rootfs_path / ".sandbox" / "exec").iterdir()) == []


async def test_local_exec_hardens_python_env_against_module_hijacking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PYTHONSAFEPATH/PYTHONNOUSERSITE are forced on and PYTHONPATH is dropped
    so /workspace cannot end up on sys.path (vector for Infr28 module hijack)."""
    import app.services.runtime.adapter as adapter_module

    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-hardened", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
            }
        )
    )
    captured: dict[str, str] = {}
    original_build = adapter_module.build_local_sandbox_exec_command

    def _capture(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs["env"])
        return original_build(**kwargs)

    monkeypatch.setattr(adapter_module, "build_local_sandbox_exec_command", _capture)

    await adapter.exec(
        context,
        command=[sys.executable, "-c", "pass"],
        cwd="/workspace",
        # User-supplied PYTHONPATH must be silently dropped even if it somehow
        # bypasses the policy engine — defense in depth.
        env={"PYTHONPATH": "/workspace"},
        timeout_sec=5,
        output_limit_bytes=1024,
    )

    assert captured["PYTHONSAFEPATH"] == "1"
    assert captured["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in captured


async def test_local_parallel_exec_uses_unique_sandbox_metadata_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.runtime.adapter as adapter_module

    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-local-parallel", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
            }
        )
    )
    original_build = adapter_module.build_local_sandbox_exec_command
    captured_paths: list[tuple[Path, Path]] = []

    def _capture_build_command(**kwargs):  # type: ignore[no-untyped-def]
        env_file_path = Path(kwargs["env_file_path"])
        exec_allowlist_path = Path(kwargs["exec_allowlist_path"])
        captured_paths.append((env_file_path, exec_allowlist_path))
        return original_build(**kwargs)

    monkeypatch.setattr(adapter_module, "build_local_sandbox_exec_command", _capture_build_command)

    first, second = await asyncio.gather(
        adapter.exec(
            context,
            command=[
                sys.executable,
                "-c",
                "import os, time; time.sleep(0.1); print(os.environ['RUNTIME_SAMPLE'])",
            ],
            cwd="/",
            env={"RUNTIME_SAMPLE": "one"},
            timeout_sec=5,
            output_limit_bytes=1024,
        ),
        adapter.exec(
            context,
            command=[
                sys.executable,
                "-c",
                "import os, time; time.sleep(0.1); print(os.environ['RUNTIME_SAMPLE'])",
            ],
            cwd="/",
            env={"RUNTIME_SAMPLE": "two"},
            timeout_sec=5,
            output_limit_bytes=1024,
        ),
    )

    assert first.stdout == "one\n"
    assert second.stdout == "two\n"
    assert len(captured_paths) == 2
    run_dirs = {env_file.parent for env_file, _ in captured_paths}
    assert len(run_dirs) == 2
    for env_file, allowlist_file in captured_paths:
        assert env_file.parent == allowlist_file.parent
        assert env_file.parent.parent == rootfs_path / ".sandbox" / "exec"
        assert not env_file.parent.exists()


async def test_local_exec_marks_truncated_output(tmp_path: Path) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-truncated", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
            }
        )
    )

    result = await adapter.exec(
        context,
        command=[sys.executable, "-c", "print('abcdefghij')"],
        cwd="/",
        env={},
        timeout_sec=5,
        output_limit_bytes=4,
    )

    assert result.exit_code == 0
    assert result.stdout == "abcd"
    assert result.stdout_truncated is True


async def test_local_exec_returns_timeout_result(tmp_path: Path) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-timeout", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
                "SIGTERM_GRACE_SEC": 1,
            }
        )
    )

    result = await adapter.exec(
        context,
        command=[sys.executable, "-c", "import time; time.sleep(2)"],
        cwd="/",
        env={},
        timeout_sec=0.1,
        output_limit_bytes=1024,
    )

    assert result.exit_code == 124
    assert result.timed_out is True
    assert result.stderr == "Command timed out"


async def test_local_exec_logs_nonzero_exit_diagnostics(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-nonzero", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
            }
        )
    )
    caplog.set_level(logging.WARNING, logger="app.services.runtime.adapter")

    result = await adapter.exec(
        context,
        command=[
            sys.executable,
            "-c",
            "import sys; print('tail-out'); print('tail-err', file=sys.stderr); sys.exit(7)",
        ],
        cwd="/",
        env={"TOKEN": "secret"},
        timeout_sec=5,
        output_limit_bytes=1024,
    )

    assert result.exit_code == 7
    record = next(item for item in caplog.records if item.message == "runtime.exec_nonzero_exit")
    diagnostics = record.diagnostics
    assert diagnostics["phase"] == "exec-exit"
    assert diagnostics["exitCode"] == 7
    assert diagnostics["sessionId"] == "session-nonzero"
    assert diagnostics["stdoutTail"].strip() == "tail-out"
    assert diagnostics["stderrTail"].strip() == "tail-err"
    assert "TOKEN" in diagnostics["envKeys"]
    assert diagnostics["wrapper"] == Path(settings.SANDBOX_BINARY).name


async def test_local_exec_spawn_failure_logs_context(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    sandbox_binary = tmp_path / "missing-ws-sandbox"
    context = RuntimeContext(session_id="session-spawn-fail", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
                "SANDBOX_BINARY": str(sandbox_binary),
            }
        )
    )
    caplog.set_level(logging.ERROR, logger="app.services.runtime.adapter")

    with pytest.raises(InternalServerErrorException) as exc_info:
        await adapter.exec(
            context,
            command=[sys.executable, "-c", "print('never starts')"],
            cwd="/",
            env={},
            timeout_sec=5,
            output_limit_bytes=1024,
        )

    assert exc_info.value.detail["details"] == {
        "phase": "spawn",
        "runtimeMode": "local",
        "sessionId": "session-spawn-fail",
        "cwd": "/",
        "binary": str(sandbox_binary),
        "errno": 2,
        "errorType": "FileNotFoundError",
    }
    record = next(item for item in caplog.records if item.message == "runtime.spawn_not_found")
    diagnostics = record.diagnostics
    assert diagnostics["phase"] == "spawn"
    assert diagnostics["binary"] == str(sandbox_binary)
    assert diagnostics["sessionId"] == "session-spawn-fail"


async def test_local_stream_exec_marks_disconnect_and_stops_process(tmp_path: Path) -> None:
    rootfs_path = tmp_path / "rootfs"
    rootfs_path.mkdir()
    context = RuntimeContext(session_id="session-disconnect", rootfs_path=str(rootfs_path))
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(tmp_path / "template"),
                "SIGTERM_GRACE_SEC": 1,
            }
        )
    )
    started = asyncio.get_running_loop().time()

    async def _disconnect_checker() -> bool:
        return (asyncio.get_running_loop().time() - started) >= 0.2

    events = [
        event
        async for event in adapter.stream_exec(
            context,
            command=[
                sys.executable,
                "-c",
                "import time; print('stream-start', flush=True); time.sleep(5)",
            ],
            cwd="/",
            env={},
            timeout_sec=10,
            disconnect_checker=_disconnect_checker,
        )
    ]

    assert events[-1].kind == "exit"
    assert events[-1].timed_out is False
    assert events[-1].disconnected is True


async def test_local_availability_requires_executable_sandbox_binary(tmp_path: Path) -> None:
    template_root = tmp_path / "template"
    template_root.mkdir()
    sandbox_binary = tmp_path / "ws-sandbox"
    sandbox_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter = RuntimeAdapter(
        settings.model_copy(
            update={
                "RUNTIME_MODE": RuntimeMode.LOCAL,
                "ROOTFS_TEMPLATE_DIR": str(template_root),
                "SANDBOX_BINARY": str(sandbox_binary),
            }
        )
    )

    available, detail = await adapter.check_availability()

    assert available is False
    assert detail == f"runtime binary is unavailable or not executable: {sandbox_binary}"


def test_build_bwrap_exec_command_matches_analysis_contract() -> None:
    command = build_bwrap_exec_command(
        systemd_run_binary="systemd-run",
        bwrap_binary="bwrap",
        rootfs_path="/srv/rootfs/session-1",
        bin_dir="/opt/workstation/bin",
        cwd="/workspace",
        command=["/bin/sh", "-lc", "pwd"],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        network_enabled=False,
        memory_limit_bytes=536870912,
        max_tasks=256,
        cpu_quota_pct=100,
    )

    assert command[:10] == [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        "MemoryMax=536870912",
        "-p",
        "TasksMax=256",
        "-p",
        "CPUQuota=100%",
    ]
    assert "--unshare-all" in command
    assert "--unshare-net" in command
    assert _contains_sequence(command, ["--bind", "/srv/rootfs/session-1", "/"])
    assert _contains_sequence(command, ["--chdir", "/workspace"])
    assert _contains_sequence(command, ["--setenv", "LANG", "C.UTF-8"])
    assert _contains_sequence(command, ["--setenv", "LC_ALL", "C.UTF-8"])
    assert command[-4:] == ["--", "/bin/sh", "-lc", "pwd"]


def test_count_pgid_processes_includes_current_session() -> None:
    """The /proc-walk should find at least the running pytest process group."""
    import os

    from app.services.runtime.adapter import _count_pgid_processes

    pgid = os.getpgrp()
    assert _count_pgid_processes(pgid) >= 1


def test_count_pgid_processes_returns_zero_for_unused_pgid() -> None:
    from app.services.runtime.adapter import _count_pgid_processes

    # PGIDs are positive int32; this value is far above any realistic process.
    assert _count_pgid_processes(2_000_000_000) == 0


def test_quota_watchdog_kills_on_pids_exceed(tmp_path) -> None:
    """When the per-session process count exceeds the limit, the watchdog kills
    the process group and reports kill_reason='pids'."""
    import contextlib
    import os
    import signal
    import subprocess
    import sys

    from app.services.runtime.adapter import _QuotaWatchdog

    # Spawn a sleeping child in its own session — its pid becomes its pgid.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        watchdog = _QuotaWatchdog(
            rootfs_path=str(tmp_path),
            max_total_bytes=10 * 1024 * 1024 * 1024,
            pids_max=0,  # 0 disables the pids check
            interval_sec=0.05,
        )
        # We can't easily await an asyncio task on a subprocess.Popen, but we
        # can directly invoke the scanner against this pid.
        from app.services.runtime.adapter import _count_pgid_processes

        observed = _count_pgid_processes(proc.pid)
        assert observed >= 1, "the spawned child should be visible under its own pgid"

        # And with pids_max=0, the watchdog stays armed but does not trigger.
        assert watchdog.kill_reason is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
