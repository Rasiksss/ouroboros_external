"""SoftSandboxBackend — same-host subprocess execution with policy enforcement.

No kernel namespacing. Network deny requires unshare --net; when it is
unavailable the run is refused (fail closed) rather than executed with network.
Filesystem positive allowlist (read_roots/write_roots) is not enforceable
without Docker — a warning is logged if those fields are set.
"""

from __future__ import annotations

import fnmatch
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from subprocess import Popen
from typing import Any

from ouroboros.contracts.sandbox_policy import NetworkPolicy, SandboxPolicy, SandboxResult
from ouroboros.platform_layer import merge_hidden_kwargs, subprocess_new_group_kwargs
from ouroboros.tools.shell import _active_subprocesses, _kill_process_group, _subprocess_lock
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def _drain_pipe_with_cap(
    pipe: Any,
    cap: int,
    buf: bytearray,
    overflow_flag: dict[str, bool],
    label: str,
) -> None:
    """Read one pipe up to cap bytes; set overflow_flag[label] on excess."""
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                return
            remaining = cap - len(buf)
            if remaining <= 0:
                overflow_flag[label] = True
                return
            if len(chunk) > remaining:
                buf.extend(chunk[:remaining])
                overflow_flag[label] = True
                return
            buf.extend(chunk)
    except (OSError, ValueError):
        return


def _resolve_cmd_arg_path(arg: str, cwd: Path) -> Path | None:
    """Resolve a cmd argv token the same way the subprocess cwd would."""
    try:
        arg_path = Path(arg)
    except Exception:
        return None
    if not arg_path.is_absolute():
        arg_path = cwd / arg_path
    try:
        return arg_path.resolve()
    except Exception:
        return None


# None = not probed yet; [] = unavailable; otherwise the working command prefix.
_unshare_net_prefix_cache: list[str] | None = None


def _unshare_net_prefix() -> list[str] | None:
    """Command prefix that puts a child in an empty network namespace, or None.

    Plain ``--net`` needs CAP_SYS_ADMIN; ``--user --net`` reaches the same namespace
    unprivileged. Completed probes are cached both ways (a missing binary or EPERM is
    a stable host property); probes that fail to complete (OSError/timeout) are
    transient: deny this run, re-probe next.
    """
    global _unshare_net_prefix_cache
    if _unshare_net_prefix_cache is not None:
        return _unshare_net_prefix_cache or None
    unshare_bin = shutil.which("unshare")
    if not unshare_bin:
        _unshare_net_prefix_cache = []
        return None
    for flags in (["--net"], ["--user", "--net"]):
        try:
            probe = subprocess.run(
                [unshare_bin, *flags, "true"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode == 0:
            _unshare_net_prefix_cache = [unshare_bin, *flags]
            return _unshare_net_prefix_cache
    _unshare_net_prefix_cache = []
    return None


def _check_deny(cmd: list[str], policy: SandboxPolicy) -> str | None:
    """Return denial reason if any cmd arg violates deny rules, else None."""
    all_deny = policy.deny_paths + policy.extra_deny_paths
    cwd = policy.cwd.resolve()
    for arg in cmd:
        try:
            arg_path = Path(arg)
        except Exception:
            continue
        if all_deny:
            resolved = _resolve_cmd_arg_path(arg, cwd)
            if resolved is None:
                continue
            for deny_p in all_deny:
                try:
                    deny_resolved = deny_p.resolve()
                    if resolved == deny_resolved or resolved.is_relative_to(deny_resolved):
                        return f"path {arg!r} is under deny_path {deny_p}"
                except Exception:
                    continue
        if policy.deny_name_patterns:
            resolved_for_name = _resolve_cmd_arg_path(arg, cwd)
            name = (resolved_for_name or arg_path).name
            for pat in policy.deny_name_patterns:
                if fnmatch.fnmatch(name, pat):
                    return f"filename {name!r} matches deny pattern {pat!r}"
    return None


class SoftSandboxBackend:
    """Same-host backend: no kernel namespacing beyond optional unshare --net."""

    def run(
        self,
        cmd: list[str],
        policy: SandboxPolicy,
        *,
        env: dict[str, str],
        stop_requested: Callable[[], bool] | None = None,
    ) -> SandboxResult:
        audit: list[dict] = []

        # 1. Static deny check — no subprocess spawn on hit.
        denial_reason = _check_deny(cmd, policy)
        if denial_reason:
            audit.append(
                {
                    "type": "sandbox.denied",
                    "ts": utc_now_iso(),
                    "reason": denial_reason,
                    "cmd": cmd[0] if cmd else "",
                }
            )
            return SandboxResult(
                stdout=b"",
                stderr=b"",
                returncode=-1,
                timed_out=False,
                overflowed=False,
                denied=True,
                denial_reason=denial_reason,
                audit_events=audit,
            )

        # Soft backend has no filesystem isolation — positive allowlist is unenforceable.
        if policy.read_roots or policy.write_roots:
            log.warning(
                "SoftSandboxBackend: read_roots/write_roots are set but will not be enforced "
                "(soft backend has no filesystem isolation; switch to docker backend for enforcement)"
            )

        # 2. Network policy. DENY is fail-closed: no enforceable isolation means no run.
        run_cmd = list(cmd)
        if policy.network == NetworkPolicy.DENY:
            unshare_prefix = _unshare_net_prefix()
            if unshare_prefix is None:
                reason = (
                    "network=deny cannot be enforced on this host "
                    "(unshare network isolation unavailable or unusable); refusing to run"
                )
                audit.append(
                    {
                        "type": "sandbox.network_deny_unenforceable",
                        "ts": utc_now_iso(),
                        "reason": reason,
                        "cmd": cmd[0] if cmd else "",
                    }
                )
                return SandboxResult(
                    stdout=b"",
                    stderr=b"",
                    returncode=-1,
                    timed_out=False,
                    overflowed=False,
                    denied=True,
                    denial_reason=reason,
                    audit_events=audit,
                )
            run_cmd = [*unshare_prefix, *run_cmd]

        if stop_requested is not None and stop_requested():
            audit.append({"type": "sandbox.cancelled", "ts": utc_now_iso(), "cmd": run_cmd[0] if run_cmd else ""})
            return SandboxResult(
                stdout=b"",
                stderr=b"",
                returncode=-1,
                timed_out=False,
                overflowed=False,
                denied=False,
                denial_reason=None,
                audit_events=audit,
                cancelled=True,
            )

        # 3. Spawn.
        audit.append(
            {
                "type": "sandbox.start",
                "ts": utc_now_iso(),
                "cmd": run_cmd[0] if run_cmd else "",
                "timeout_sec": policy.timeout_sec,
            }
        )

        popen_kwargs: dict[str, Any] = {
            "cwd": str(policy.cwd),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
        }
        popen_kwargs.update(subprocess_new_group_kwargs())
        popen_kwargs = merge_hidden_kwargs(popen_kwargs)

        proc = Popen(run_cmd, **popen_kwargs)
        with _subprocess_lock:
            _active_subprocesses.add(proc)

        stdout_buf = bytearray()
        stderr_buf = bytearray()
        overflow_flag: dict[str, bool] = {"stdout": False, "stderr": False}

        stdout_thread = threading.Thread(
            target=_drain_pipe_with_cap,
            args=(proc.stdout, policy.stdout_cap, stdout_buf, overflow_flag, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_pipe_with_cap,
            args=(proc.stderr, policy.stderr_cap, stderr_buf, overflow_flag, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        # 4. Wait with timeout and overflow kill.
        deadline = time.monotonic() + max(1, int(policy.timeout_sec))
        overflowed = False
        timed_out = False
        cancelled = False
        stop_error: Exception | None = None
        try:
            while True:
                if proc.poll() is not None:
                    break
                if stop_requested is not None:
                    try:
                        should_stop = stop_requested()
                    except Exception as exc:
                        log.warning("Sandbox stop predicate raised; killing process", exc_info=True)
                        stop_error = exc
                        should_stop = True
                else:
                    should_stop = False
                if should_stop:
                    cancelled = True
                    audit.append(
                        {"type": "sandbox.cancelled", "ts": utc_now_iso(), "cmd": run_cmd[0] if run_cmd else ""}
                    )
                    _kill_process_group(proc)
                    break
                if overflow_flag["stdout"] or overflow_flag["stderr"]:
                    overflowed = True
                    _kill_process_group(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_process_group(proc)
                    break
                time.sleep(0.05)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                proc.wait(timeout=2)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
        finally:
            with _subprocess_lock:
                _active_subprocesses.discard(proc)
            try:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
            except OSError:
                pass

        if stop_error is not None:
            raise stop_error

        returncode = proc.returncode or 0

        # 5. Finish audit event.
        audit.append(
            {
                "type": "sandbox.finish",
                "ts": utc_now_iso(),
                "returncode": returncode,
                "timed_out": timed_out,
                "overflowed": overflowed,
                "cancelled": cancelled,
            }
        )

        return SandboxResult(
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
            returncode=returncode,
            timed_out=timed_out,
            overflowed=overflowed,
            denied=False,
            denial_reason=None,
            audit_events=audit,
            cancelled=cancelled,
        )


__all__ = ["SoftSandboxBackend"]
