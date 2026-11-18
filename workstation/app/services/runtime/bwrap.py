"""Builders for the rootless bwrap runtime command line."""

from __future__ import annotations

from collections.abc import Mapping

from app.utils.exceptions.common import ValidationErrorException
from app.utils.paths import normalize_virtual_path

DEFAULT_EXEC_PATH = "/usr/local/bin:/usr/bin:/bin"


def build_bwrap_exec_command(
    *,
    systemd_run_binary: str,
    bwrap_binary: str,
    rootfs_path: str,
    bin_dir: str,
    cwd: str,
    command: list[str],
    env: Mapping[str, str],
    network_enabled: bool,
    memory_limit_bytes: int,
    max_tasks: int,
    cpu_quota_pct: int,
) -> list[str]:
    if not command:
        raise ValidationErrorException("command must not be empty", details={"field": "command"})

    normalized_cwd = normalize_virtual_path(cwd)
    runtime_cmd = [
        systemd_run_binary,
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={memory_limit_bytes}",
        "-p",
        f"TasksMax={max_tasks}",
        "-p",
        f"CPUQuota={cpu_quota_pct}%",
        "--",
        bwrap_binary,
        "--unshare-all",
        "--share-net" if network_enabled else "--unshare-net",
        "--die-with-parent",
        "--bind",
        rootfs_path,
        "/",
        # Shared, read-only toolchain. Mirrors the local-runtime layout: the
        # session rootfs does not contain bin/ at all.
        "--ro-bind",
        bin_dir,
        "/bin",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        normalized_cwd,
        "--setenv",
        "PATH",
        DEFAULT_EXEC_PATH,
    ]

    for key, value in sorted(env.items()):
        runtime_cmd.extend(["--setenv", key, value])

    runtime_cmd.extend(["--", *command])
    return runtime_cmd
