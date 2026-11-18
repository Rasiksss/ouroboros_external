"""Builders for the local ws-sandbox runtime command line."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from app.utils.exceptions.common import ValidationErrorException
from app.utils.paths import normalize_virtual_path


def resolve_local_exec_command(command: list[str], exec_allowlist: Sequence[str]) -> list[str]:
    if not command:
        raise ValidationErrorException("command must not be empty", details={"field": "command"})

    executable = command[0]
    if "/" in executable:
        return list(command)

    matches = [path for path in exec_allowlist if PurePosixPath(path).name == executable]
    if not matches:
        raise ValidationErrorException(
            "command executable is not in the local exec allowlist",
            details={"command": executable},
        )
    if len(matches) > 1:
        raise ValidationErrorException(
            "command executable is ambiguous in the local exec allowlist",
            details={"command": executable, "matches": matches},
        )
    return [matches[0], *command[1:]]


def build_local_sandbox_exec_command(
    *,
    sandbox_binary: str,
    session_id: str,
    rootfs_path: str,
    bin_dir: str,
    cwd: str,
    command: list[str],
    env: Mapping[str, str],
    memory_limit_bytes: int,
    pids_max: int,
    cpu_quota_pct: int,
    rlimit_as: int,
    rlimit_fsize: int,
    rlimit_cpu_sec: int,
    rlimit_nofile: int,
    rlimit_nproc: int,
    env_file_path: str,
    exec_allowlist_path: str,
    network_enabled: bool = False,
    exec_allowlist: Sequence[str] = (),
) -> list[str]:
    if not command:
        raise ValidationErrorException("command must not be empty", details={"field": "command"})

    normalized_cwd = normalize_virtual_path(cwd)
    _write_env_file(Path(env_file_path), env)
    _write_exec_allowlist(Path(exec_allowlist_path), exec_allowlist)

    return [
        sandbox_binary,
        "--session-id",
        session_id,
        "--session-dir",
        rootfs_path,
        "--bin-dir",
        bin_dir,
        "--cwd",
        normalized_cwd,
        "--memory-max",
        str(memory_limit_bytes),
        "--pids-max",
        str(pids_max),
        "--cpu-quota-pct",
        str(cpu_quota_pct),
        "--rlimit-as",
        str(rlimit_as),
        "--rlimit-fsize",
        str(rlimit_fsize),
        "--rlimit-cpu",
        str(rlimit_cpu_sec),
        "--rlimit-nofile",
        str(rlimit_nofile),
        "--rlimit-nproc",
        str(rlimit_nproc),
        "--rlimit-stack",
        "0",
        "--rlimit-core",
        "0",
        "--rlimit-memlock",
        "0",
        "--env-file",
        env_file_path,
        "--exec-allowlist",
        exec_allowlist_path,
        "--network-enabled",
        "1" if network_enabled else "0",
        "--",
        *command,
    ]


def _write_env_file(path: Path, env: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in sorted(env.items()):
        _validate_env_item(key, value)
        lines.append(f"{key}={value}")
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _write_exec_allowlist(path: Path, exec_allowlist: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for item in exec_allowlist:
        if not item or "\n" in item or "\x00" in item:
            raise ValidationErrorException(
                "exec allowlist entries must be non-empty single-line paths",
                details={"path": item},
            )
        lines.append(item)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _validate_env_item(key: str, value: str) -> None:
    if not key or "=" in key or "\n" in key or "\x00" in key:
        raise ValidationErrorException(
            "environment variable names must be non-empty single-line keys",
            details={"key": key},
        )
    if "\n" in value or "\x00" in value:
        raise ValidationErrorException(
            "environment variable values must be single-line strings",
            details={"key": key},
        )
