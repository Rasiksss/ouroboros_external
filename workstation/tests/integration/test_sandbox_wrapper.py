"""Integration checks for the ws-sandbox wrapper binary scaffold."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_DIR = REPO_ROOT / "wrapper"
WRAPPER_BINARY = WRAPPER_DIR / "build" / "ws-sandbox"


def _require_build_tools() -> None:
    if shutil.which("make") is None:
        pytest.skip("make is not available")
    if shutil.which("cc") is None and shutil.which("musl-gcc") is None:
        pytest.skip("no C compiler is available")


def _build_wrapper() -> None:
    _require_build_tools()
    result = subprocess.run(
        ["make"],
        cwd=WRAPPER_DIR,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert WRAPPER_BINARY.exists()


def _base_args(tmp_path: Path) -> list[str]:
    session_dir = tmp_path / "session"
    bin_dir = tmp_path / "bin"
    workspace = session_dir / "workspace"
    workspace.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    command = ["/bin/sh", "-lc", 'printf \'%s:%s\' "$PWD" "$LANG"']
    if platform.system() == "Linux":
        _install_shared_binary(bin_dir, "/bin/sh")
    else:
        script = workspace / "run.sh"
        script.write_text('#!/bin/sh\nprintf \'%s:%s\' "$PWD" "$LANG"\n', encoding="utf-8")
        script.chmod(0o755)
        command = ["./run.sh"]
    sandbox_dir = session_dir / ".sandbox"
    sandbox_dir.mkdir()
    env_file = sandbox_dir / "env"
    allowlist_file = sandbox_dir / "exec.allow"
    env_file.write_text(f"LANG=C.UTF-8\nPATH={bin_dir}\n", encoding="utf-8")
    allowlist_file.write_text("/bin/sh\n", encoding="utf-8")
    return _wrapper_args(
        session_dir=session_dir,
        bin_dir=bin_dir,
        env_file=env_file,
        allowlist_file=allowlist_file,
        command=command,
    )


def _install_shared_binary(bin_dir: Path, virtual_path: str) -> None:
    source = Path(virtual_path)
    target = bin_dir / Path(virtual_path).name
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target, follow_symlinks=True)
    target.chmod(source.stat().st_mode & 0o777)


def _wrapper_args(
    *,
    session_dir: Path,
    bin_dir: Path,
    env_file: Path,
    allowlist_file: Path,
    command: list[str],
) -> list[str]:
    return [
        str(WRAPPER_BINARY),
        "--session-id",
        "test-session",
        "--session-dir",
        str(session_dir),
        "--bin-dir",
        str(bin_dir),
        "--cwd",
        "/workspace",
        "--memory-max",
        "0",
        "--pids-max",
        "0",
        "--cpu-quota-pct",
        "0",
        "--rlimit-as",
        "0",
        "--rlimit-fsize",
        "0",
        "--rlimit-cpu",
        "0",
        "--rlimit-nofile",
        "0",
        "--rlimit-nproc",
        "0",
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
        "--",
        *command,
    ]


def _linux_only() -> None:
    if platform.system() != "Linux":
        pytest.skip("Linux-only wrapper security behavior")


def test_wrapper_make_builds_binary() -> None:
    _build_wrapper()


def test_wrapper_make_clean_removes_build_dir() -> None:
    _build_wrapper()

    result = subprocess.run(
        ["make", "clean"],
        cwd=WRAPPER_DIR,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (WRAPPER_DIR / "build").exists()


def test_wrapper_help_exits_zero() -> None:
    _build_wrapper()

    result = subprocess.run(
        [str(WRAPPER_BINARY), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: ws-sandbox" in result.stdout
    assert result.stderr == ""


def test_wrapper_parser_error_exits_usage() -> None:
    _build_wrapper()

    result = subprocess.run(
        [str(WRAPPER_BINARY), "--session-id"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "error:" in result.stderr
    assert "usage: ws-sandbox" in result.stderr


def test_wrapper_valid_args_executes_command_with_env_and_cwd(tmp_path: Path) -> None:
    _build_wrapper()

    result = subprocess.run(
        _base_args(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == f"{tmp_path / 'session' / 'workspace'}:C.UTF-8"


def test_wrapper_exec_failure_includes_diagnostic_context(tmp_path: Path) -> None:
    _build_wrapper()
    args = _base_args(tmp_path)
    command_index = args.index("--") + 1
    args[command_index:] = ["/missing-command"]

    result = subprocess.run(
        args,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "WRAPPER: error step=execve" in result.stderr
    assert "errno_text=" in result.stderr
    assert 'msg="failed to execute command"' in result.stderr
    assert "WRAPPER: context" in result.stderr
    assert 'session_id="test-session"' in result.stderr
    assert 'cwd="/workspace"' in result.stderr
    assert 'command="/missing-command"' in result.stderr


def test_wrapper_seccomp_blocks_foreign_pid_kill_but_allows_current_pid_and_pgid(tmp_path: Path) -> None:
    _linux_only()
    _build_wrapper()
    session_dir = tmp_path / "session"
    bin_dir = tmp_path / "bin"
    sandbox_dir = session_dir / ".sandbox"
    (session_dir / "workspace").mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    _install_shared_binary(bin_dir, "/bin/sh")
    sandbox_dir.mkdir()
    env_file = sandbox_dir / "env"
    allowlist_file = sandbox_dir / "exec.allow"
    env_file.write_text(f"LANG=C.UTF-8\nPATH={bin_dir}\n", encoding="utf-8")
    allowlist_file.write_text("/bin/sh\n", encoding="utf-8")

    result = subprocess.run(
        _wrapper_args(
            session_dir=session_dir,
            bin_dir=bin_dir,
            env_file=env_file,
            allowlist_file=allowlist_file,
            command=[
                "/bin/sh",
                "-lc",
                (
                    "self=$$; "
                    "kill -0 1; printf 'kill1:%s:' \"$?\"; "
                    "kill -0 0; printf 'kill0:%s:' \"$?\"; "
                    'kill -0 "$self"; printf \'killself:%s\' "$?"'
                ),
            ],
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "kill1:1:kill0:0:killself:0"
    assert "Operation not permitted" in result.stderr


def test_wrapper_landlock_blocks_other_session_host_file_and_uploaded_exec(tmp_path: Path) -> None:
    _linux_only()
    _build_wrapper()
    session_dir = tmp_path / "session"
    bin_dir = tmp_path / "bin"
    other_dir = tmp_path / "other"
    sandbox_dir = session_dir / ".sandbox"
    workspace = session_dir / "workspace"
    workspace.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    _install_shared_binary(bin_dir, "/bin/sh")
    _install_shared_binary(bin_dir, "/bin/cat")
    other_dir.mkdir()
    sandbox_dir.mkdir()
    (workspace / "own").write_text("own", encoding="utf-8")
    (other_dir / "secret").write_text("secret", encoding="utf-8")
    uploaded = workspace / "x"
    uploaded.write_text("#!/bin/sh\necho uploaded\n", encoding="utf-8")
    uploaded.chmod(0o755)
    env_file = sandbox_dir / "env"
    allowlist_file = sandbox_dir / "exec.allow"
    env_file.write_text(f"LANG=C.UTF-8\nPATH={bin_dir}\n", encoding="utf-8")
    allowlist_file.write_text("/bin/sh\n/bin/cat\n", encoding="utf-8")

    result = subprocess.run(
        _wrapper_args(
            session_dir=session_dir,
            bin_dir=bin_dir,
            env_file=env_file,
            allowlist_file=allowlist_file,
            command=[
                "/bin/sh",
                "-lc",
                (
                    "cat own; printf ':own=%s:' \"$?\"; "
                    f"cat {other_dir / 'secret'}; printf 'other=%s:' \"$?\"; "
                    "cat /etc/passwd; printf 'etc=%s:' \"$?\"; "
                    "cat /proc/1/environ; printf 'proc=%s:' \"$?\"; "
                    "./x; printf 'exec=%s' \"$?\""
                ),
            ],
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "own:own=0:other=1:etc=1:proc=1:exec=126"
    assert "Permission denied" in result.stderr


def test_wrapper_landlock_blocks_shell_payload_commands_outside_exec_allowlist(tmp_path: Path) -> None:
    _linux_only()
    if not Path("/bin/bash").exists():
        pytest.skip("bash is not available")
    _build_wrapper()
    session_dir = tmp_path / "session"
    bin_dir = tmp_path / "bin"
    sandbox_dir = session_dir / ".sandbox"
    workspace = session_dir / "workspace"
    workspace.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    for virtual_path in ("/bin/bash", "/bin/cat", "/bin/sh", "/bin/rm"):
        _install_shared_binary(bin_dir, virtual_path)
    sandbox_dir.mkdir()
    (workspace / "own").write_text("own", encoding="utf-8")
    env_file = sandbox_dir / "env"
    allowlist_file = sandbox_dir / "exec.allow"
    env_file.write_text(f"PATH={bin_dir}\n", encoding="utf-8")
    allowlist_file.write_text("/bin/bash\n/bin/cat\n", encoding="utf-8")

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            _wrapper_args(
                session_dir=session_dir,
                bin_dir=bin_dir,
                env_file=env_file,
                allowlist_file=allowlist_file,
                command=command,
            ),
            text=True,
            capture_output=True,
            check=False,
        )

    direct_cat = run(["/bin/cat", "own"])
    assert direct_cat.returncode == 0
    assert direct_cat.stdout == "own"

    shell_cat = run(["/bin/bash", "-c", "cat own"])
    assert shell_cat.returncode == 0
    assert shell_cat.stdout == "own"

    direct_rm = run(["/bin/rm", "--version"])
    assert direct_rm.returncode != 0
    assert "failed to execute command" in direct_rm.stderr

    direct_sh = run(["/bin/sh", "-c", "cat own"])
    assert direct_sh.returncode != 0
    assert "failed to execute command" in direct_sh.stderr

    shell_rm = run(["/bin/bash", "-c", "rm --version"])
    assert shell_rm.returncode != 0
    assert "Permission denied" in shell_rm.stderr

    shell_sh_cat = run(["/bin/bash", "-c", "sh -c 'cat own'"])
    assert shell_sh_cat.returncode != 0
    assert shell_sh_cat.stdout != "own"
    assert "Permission denied" in shell_sh_cat.stderr

    shell_abs_sh_cat = run(["/bin/bash", "-c", "/bin/sh -c 'cat own'"])
    assert shell_abs_sh_cat.returncode != 0
    assert shell_abs_sh_cat.stdout != "own"
    assert "Permission denied" in shell_abs_sh_cat.stderr
