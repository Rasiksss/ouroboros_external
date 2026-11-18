"""Unit tests for the local ws-sandbox runtime builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.runtime.sandbox import build_local_sandbox_exec_command, resolve_local_exec_command
from app.utils.exceptions.common import ValidationErrorException


def test_resolve_local_exec_command_maps_bare_name_from_allowlist() -> None:
    command = resolve_local_exec_command(
        ["bash", "-lc", "pwd"],
        ["/bin/bash", "/bin/cat", "/bin/ls", "/bin/pwd"],
    )

    assert command == ["/bin/bash", "-lc", "pwd"]


def test_resolve_local_exec_command_keeps_path_commands_unchanged() -> None:
    allowlist = ["/bin/bash", "/bin/ls"]

    assert resolve_local_exec_command(["/bin/ls", "-la"], allowlist) == ["/bin/ls", "-la"]
    assert resolve_local_exec_command(["./x"], allowlist) == ["./x"]
    assert resolve_local_exec_command(["tools/x"], allowlist) == ["tools/x"]


def test_resolve_local_exec_command_rejects_unknown_bare_name() -> None:
    with pytest.raises(ValidationErrorException, match="not in the local exec allowlist"):
        resolve_local_exec_command(["python", "-V"], ["/bin/bash", "/bin/ls"])


def test_resolve_local_exec_command_rejects_ambiguous_bare_name() -> None:
    with pytest.raises(ValidationErrorException, match="ambiguous"):
        resolve_local_exec_command(["env"], ["/bin/env", "/usr/bin/env"])


def test_build_local_sandbox_exec_command_writes_files_and_argv(tmp_path: Path) -> None:
    env_file = tmp_path / "rootfs" / ".sandbox" / "exec" / "run-1" / "env"
    allowlist_file = tmp_path / "rootfs" / ".sandbox" / "exec" / "run-1" / "exec.allow"

    command = build_local_sandbox_exec_command(
        sandbox_binary="/usr/local/bin/ws-sandbox",
        session_id="session-1",
        rootfs_path=str(tmp_path / "rootfs"),
        bin_dir="/opt/workstation/bin",
        cwd="/workspace",
        command=["/bin/bash", "-lc", "pwd"],
        env={"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
        memory_limit_bytes=536870912,
        pids_max=64,
        cpu_quota_pct=50,
        rlimit_as=1073741824,
        rlimit_fsize=104857600,
        rlimit_cpu_sec=60,
        rlimit_nofile=256,
        rlimit_nproc=1024,
        env_file_path=str(env_file),
        exec_allowlist_path=str(allowlist_file),
        exec_allowlist=["/bin/bash", "/bin/cat", "/bin/ls"],
    )

    assert command == [
        "/usr/local/bin/ws-sandbox",
        "--session-id",
        "session-1",
        "--session-dir",
        str(tmp_path / "rootfs"),
        "--bin-dir",
        "/opt/workstation/bin",
        "--cwd",
        "/workspace",
        "--memory-max",
        "536870912",
        "--pids-max",
        "64",
        "--cpu-quota-pct",
        "50",
        "--rlimit-as",
        "1073741824",
        "--rlimit-fsize",
        "104857600",
        "--rlimit-cpu",
        "60",
        "--rlimit-nofile",
        "256",
        "--rlimit-nproc",
        "1024",
        "--rlimit-stack",
        "0",
        "--rlimit-core",
        "0",
        "--rlimit-memlock",
        "0",
        "--env-file",
        str(env_file),
        "--exec-allowlist",
        str(allowlist_file),
        "--network-enabled",
        "0",
        "--",
        "/bin/bash",
        "-lc",
        "pwd",
    ]
    assert env_file.read_text(encoding="utf-8") == "LANG=C.UTF-8\nLC_ALL=C.UTF-8\n"
    assert allowlist_file.read_text(encoding="utf-8") == "/bin/bash\n/bin/cat\n/bin/ls\n"


def test_build_local_sandbox_exec_command_rejects_empty_command(tmp_path: Path) -> None:
    with pytest.raises(ValidationErrorException, match="command must not be empty"):
        build_local_sandbox_exec_command(
            sandbox_binary="/usr/local/bin/ws-sandbox",
            session_id="session-1",
            rootfs_path=str(tmp_path / "rootfs"),
            bin_dir="/opt/workstation/bin",
            cwd="/workspace",
            command=[],
            env={},
            memory_limit_bytes=0,
            pids_max=0,
            cpu_quota_pct=0,
            rlimit_as=0,
            rlimit_fsize=0,
            rlimit_cpu_sec=0,
            rlimit_nofile=0,
            rlimit_nproc=0,
            env_file_path=str(tmp_path / ".sandbox" / "env"),
            exec_allowlist_path=str(tmp_path / ".sandbox" / "exec.allow"),
        )


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("workspace", "/workspace"),
        ("/workspace/./src", "/workspace/src"),
        ("", "/"),
    ],
)
def test_build_local_sandbox_exec_command_normalizes_cwd(tmp_path: Path, cwd: str, expected: str) -> None:
    command = build_local_sandbox_exec_command(
        sandbox_binary="/usr/local/bin/ws-sandbox",
        session_id="session-1",
        rootfs_path=str(tmp_path / "rootfs"),
        bin_dir="/opt/workstation/bin",
        cwd=cwd,
        command=["/bin/pwd"],
        env={},
        memory_limit_bytes=0,
        pids_max=0,
        cpu_quota_pct=0,
        rlimit_as=0,
        rlimit_fsize=0,
        rlimit_cpu_sec=0,
        rlimit_nofile=0,
        rlimit_nproc=0,
        env_file_path=str(tmp_path / ".sandbox" / "env"),
        exec_allowlist_path=str(tmp_path / ".sandbox" / "exec.allow"),
    )

    assert command[command.index("--cwd") + 1] == expected


def test_build_local_sandbox_exec_command_keeps_zero_limits(tmp_path: Path) -> None:
    command = build_local_sandbox_exec_command(
        sandbox_binary="/usr/local/bin/ws-sandbox",
        session_id="session-1",
        rootfs_path=str(tmp_path / "rootfs"),
        bin_dir="/opt/workstation/bin",
        cwd="/",
        command=["/bin/pwd"],
        env={},
        memory_limit_bytes=0,
        pids_max=0,
        cpu_quota_pct=0,
        rlimit_as=0,
        rlimit_fsize=0,
        rlimit_cpu_sec=0,
        rlimit_nofile=0,
        rlimit_nproc=0,
        env_file_path=str(tmp_path / ".sandbox" / "env"),
        exec_allowlist_path=str(tmp_path / ".sandbox" / "exec.allow"),
    )

    for flag in (
        "--memory-max",
        "--pids-max",
        "--cpu-quota-pct",
        "--rlimit-as",
        "--rlimit-fsize",
        "--rlimit-cpu",
        "--rlimit-nofile",
        "--rlimit-nproc",
    ):
        assert command[command.index(flag) + 1] == "0"


def test_build_local_sandbox_exec_command_rejects_multiline_env(tmp_path: Path) -> None:
    with pytest.raises(ValidationErrorException, match="single-line"):
        build_local_sandbox_exec_command(
            sandbox_binary="/usr/local/bin/ws-sandbox",
            session_id="session-1",
            rootfs_path=str(tmp_path / "rootfs"),
            bin_dir="/opt/workstation/bin",
            cwd="/",
            command=["/bin/pwd"],
            env={"BAD": "line1\nline2"},
            memory_limit_bytes=0,
            pids_max=0,
            cpu_quota_pct=0,
            rlimit_as=0,
            rlimit_fsize=0,
            rlimit_cpu_sec=0,
            rlimit_nofile=0,
            rlimit_nproc=0,
            env_file_path=str(tmp_path / ".sandbox" / "env"),
            exec_allowlist_path=str(tmp_path / ".sandbox" / "exec.allow"),
        )
