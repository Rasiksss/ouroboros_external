"""Runtime adapter for provisioning and command execution."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import shutil
import signal
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from shutil import which
from typing import Any

from app.core.config import RuntimeMode, Settings, settings
from app.services.runtime.bwrap import build_bwrap_exec_command
from app.services.runtime.sandbox import build_local_sandbox_exec_command, resolve_local_exec_command
from app.services.runtime.types import ExecResult, ExecStreamEvent, RuntimeContext
from app.utils.disk import calculate_total_bytes
from app.utils.exceptions.common import InternalServerErrorException, ValidationErrorException
from app.utils.logging import redact_log_value
from app.utils.paths import map_virtual_to_host, normalize_virtual_path

logger = logging.getLogger(__name__)
CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")
_WRAPPER_WARNING_RE = re.compile(r"^WRAPPER: warning step=(?P<step>[A-Za-z0-9_]+)\b")
_OUTPUT_TAIL_BYTES = 4096


@dataclass(slots=True, frozen=True)
class _RuntimeProcessSpec:
    argv: list[str]
    process_cwd: str | None
    process_env: dict[str, str] | None
    normalized_cwd: str
    diagnostics: dict[str, Any]
    cleanup_paths: tuple[Path, ...] = ()


class RuntimeAdapter:
    """Filesystem provisioning and process execution for session runtimes."""

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or settings

    async def provision_rootfs(self, rootfs_path: str) -> None:
        op = "RuntimeAdapter.provision_rootfs"
        started = time.perf_counter()
        template_root = Path(self._settings.ROOTFS_TEMPLATE_DIR)
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                "diagnostics": {
                    "phase": "runtime-provision-start",
                    "runtimeMode": self._settings.RUNTIME_MODE.value,
                    "rootfsPath": rootfs_path,
                    "templateRoot": str(template_root),
                },
            },
        )
        rootfs = Path(rootfs_path)
        shutil.rmtree(rootfs, ignore_errors=True)

        try:
            if not template_root.exists():
                raise InternalServerErrorException(
                    "ROOTFS template directory does not exist",
                    details={"path": str(template_root)},
                )
            if not template_root.is_dir():
                raise InternalServerErrorException(
                    "ROOTFS template path must point to a directory",
                    details={"path": str(template_root)},
                )
            shutil.copytree(template_root, rootfs, symlinks=True)
        except OSError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "event.reason": "failed to provision session rootfs",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                },
            )
            raise InternalServerErrorException(
                "failed to provision session rootfs",
                details={"rootfsPath": rootfs_path},
                additional=str(exc),
            ) from exc
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )

    async def check_availability(self) -> tuple[bool, str]:
        template_root = Path(self._settings.ROOTFS_TEMPLATE_DIR)
        if not template_root.exists():
            return self._availability_result(False, f"template root is missing: {template_root}")
        if not template_root.is_dir():
            return self._availability_result(False, f"template root is not a directory: {template_root}")

        if self._settings.RUNTIME_MODE == RuntimeMode.LOCAL:
            if not _binary_is_executable(self._settings.SANDBOX_BINARY):
                return self._availability_result(
                    False,
                    f"runtime binary is unavailable or not executable: {self._settings.SANDBOX_BINARY}",
                )
            return self._availability_result(True, "ok")

        if self._settings.RUNTIME_MODE == RuntimeMode.BWRAP:
            if not _binary_exists(self._settings.BWRAP_BINARY):
                return self._availability_result(
                    False,
                    f"runtime binary is unavailable: {self._settings.BWRAP_BINARY}",
                )
            if not _binary_exists(self._settings.SYSTEMD_RUN_BINARY):
                return self._availability_result(
                    False,
                    f"runtime binary is unavailable: {self._settings.SYSTEMD_RUN_BINARY}",
                )
            available, detail = await self._check_systemd_user_scope()
            return self._availability_result(available, detail)

        return self._availability_result(False, f"unsupported runtime mode: {self._settings.RUNTIME_MODE}")

    async def destroy_rootfs(self, rootfs_path: str) -> None:
        op = "RuntimeAdapter.destroy_rootfs"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "processing",
                "event.action": "process",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        try:
            shutil.rmtree(rootfs_path, ignore_errors=True)
            if self._settings.RUNTIME_MODE == RuntimeMode.LOCAL:
                _cleanup_local_cgroup(rootfs_path)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "process",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                },
            )

    def audit_context(self, *, stderr: str | None = None) -> dict[str, str]:
        payload = {"runtime": self._settings.RUNTIME_MODE.value}
        if self._settings.RUNTIME_MODE != RuntimeMode.LOCAL:
            return payload

        payload["wrapper"] = Path(self._settings.SANDBOX_BINARY).name
        if stderr is None:
            return payload

        skipped_steps = _wrapper_warning_steps(stderr)
        payload["landlock"] = "skipped" if "landlock" in skipped_steps else "applied"
        payload["seccomp"] = "skipped" if "seccomp" in skipped_steps else "applied"
        payload["cgroup"] = "skipped" if "cgroup" in skipped_steps else "applied"
        return payload

    async def exec(
        self,
        context: RuntimeContext,
        *,
        command: list[str],
        cwd: str,
        env: Mapping[str, str],
        timeout_sec: int,
        output_limit_bytes: int,
        stdin: str | bytes | None = None,
    ) -> ExecResult:
        process = self._prepare_process(
            context=context,
            command=command,
            cwd=cwd,
            env=env,
        )
        try:
            return await self._run_exec(
                process=process,
                stdin=stdin,
                timeout_sec=timeout_sec,
                output_limit_bytes=output_limit_bytes,
                rootfs_path=context.rootfs_path,
            )
        finally:
            _cleanup_paths(process.cleanup_paths)

    async def stream_exec(
        self,
        context: RuntimeContext,
        *,
        command: list[str],
        cwd: str,
        env: Mapping[str, str],
        timeout_sec: int,
        stdin: str | bytes | None = None,
        disconnect_checker: Callable[[], Awaitable[bool]] | None = None,
    ):
        process = self._prepare_process(
            context=context,
            command=command,
            cwd=cwd,
            env=env,
        )
        stdin_bytes = _encode_stdin(stdin)
        try:
            proc = await self._spawn_process(
                process=process,
                stdin=stdin_bytes,
            )
        except Exception:
            _cleanup_paths(process.cleanup_paths)
            raise
        await self._write_stdin(proc, stdin_bytes)
        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        stdout_task = asyncio.create_task(self._pump_stream(proc.stdout, "stdout", queue))
        stderr_task = asyncio.create_task(self._pump_stream(proc.stderr, "stderr", queue))
        watchdog = _QuotaWatchdog(
            rootfs_path=context.rootfs_path,
            max_total_bytes=self._settings.MAX_TOTAL_BYTES,
            pids_max=self._settings.SESSION_PIDS_MAX,
            interval_sec=self._settings.SESSION_FILE_WATCHDOG_INTERVAL_SEC,
        )
        watchdog_task = asyncio.create_task(watchdog.run(proc, process.diagnostics))
        timeout_deadline = time.perf_counter() + timeout_sec
        started_at = time.perf_counter()
        timed_out = False
        disconnected = False
        completed_streams = 0
        stdout_tail = _TailTextBuffer()
        stderr_tail = _TailTextBuffer()

        op = "RuntimeAdapter.stream_exec"
        logger.debug(
            "runtime.stream_exec mode=%s session_id=%s cwd=%s env_keys=%s command=%s",
            self._settings.RUNTIME_MODE.value,
            context.session_id,
            process.normalized_cwd,
            sorted(env.keys()),
            process.diagnostics["argv"],
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
                "diagnostics": {
                    **process.diagnostics,
                    "phase": "stream-start",
                    "timeoutSec": timeout_sec,
                    "stdinBytes": len(stdin_bytes or b""),
                },
            },
        )
        try:
            while completed_streams < 2 or not queue.empty() or proc.returncode is None:
                if disconnect_checker is not None and await disconnect_checker():
                    disconnected = True
                    await self._terminate_process(proc)
                    break

                if proc.returncode is None and time.perf_counter() >= timeout_deadline:
                    timed_out = True
                    await self._terminate_process(proc)
                    break

                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue

                if item is None:
                    completed_streams += 1
                    continue

                kind, data = item
                if kind == "stdout":
                    stdout_tail.append(data)
                elif kind == "stderr":
                    stderr_tail.append(data)
                yield ExecStreamEvent(kind=kind, data=data)
        finally:
            try:
                await proc.wait()
                await _gather_safely(stdout_task, stderr_task)
                watchdog_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watchdog_task
            finally:
                _cleanup_paths(process.cleanup_paths)

        while not queue.empty():
            item = queue.get_nowait()
            if item is None:
                continue
            kind, data = item
            if kind == "stdout":
                stdout_tail.append(data)
            elif kind == "stderr":
                stderr_tail.append(data)
            yield ExecStreamEvent(kind=kind, data=data)

        kill_reason = watchdog.kill_reason
        exit_event = ExecStreamEvent(
            kind="exit",
            exit_code=124 if timed_out else (proc.returncode or 0),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            timed_out=timed_out,
            disconnected=disconnected or None,
            kill_reason=kill_reason,
        )
        exit_diagnostics = {
            **process.diagnostics,
            "phase": "stream-exit",
            "exitCode": exit_event.exit_code,
            "durationMs": exit_event.duration_ms,
            "timedOut": exit_event.timed_out,
            "disconnected": bool(exit_event.disconnected),
            "stdoutTail": stdout_tail.value,
            "stderrTail": stderr_tail.value,
        }
        if exit_event.exit_code != 0 or exit_event.timed_out or exit_event.disconnected:
            logger.warning(
                "runtime.stream_exec_failed",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": exit_event.duration_ms,
                    "diagnostics": exit_diagnostics,
                },
            )
        else:
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "processing",
                    "event.action": "complete",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": exit_event.duration_ms,
                    "diagnostics": exit_diagnostics,
                },
            )
        yield exit_event

    def _prepare_process(
        self,
        *,
        context: RuntimeContext,
        command: list[str],
        cwd: str,
        env: Mapping[str, str],
    ) -> _RuntimeProcessSpec:
        if not command:
            raise ValidationErrorException("command must not be empty", details={"field": "command"})

        normalized_cwd = normalize_virtual_path(cwd)
        env_overrides = {key: value for key, value in env.items()}
        # Harden CPython against module-hijacking via the session workspace.
        # PYTHONSAFEPATH blocks the default sys.path[0] = script-dir/cwd prepend
        # (so a planted /workspace/subprocess.py cannot shadow the stdlib),
        # PYTHONNOUSERSITE blocks the ~/.local/lib/... site dir, and we drop
        # any user-supplied PYTHONPATH so an explicit path cannot reintroduce
        # the same vector. Set unconditionally — these vars are harmless for
        # non-Python commands.
        env_overrides["PYTHONSAFEPATH"] = "1"
        env_overrides["PYTHONNOUSERSITE"] = "1"
        env_overrides.pop("PYTHONPATH", None)

        if self._settings.RUNTIME_MODE == RuntimeMode.LOCAL:
            host_cwd = map_virtual_to_host(context.rootfs_path, normalized_cwd)
            host_cwd.mkdir(parents=True, exist_ok=True)
            sandbox_dir = Path(context.rootfs_path) / ".sandbox"
            exec_dir = sandbox_dir / "exec" / uuid.uuid4().hex
            # Toolchain lives in the shared, read-only bin dir (Landlock-enforced
            # rx-only). PATH points at it so shells resolve commands there and
            # not at host /usr/bin or /bin.
            shared_bin = self._settings.SHARED_BIN_DIR
            client_path = env_overrides.get("PATH", "").strip()
            env_overrides["PATH"] = f"{shared_bin}:{client_path}" if client_path else shared_bin
            resolved_command = resolve_local_exec_command(list(command), self._settings.EXEC_ALLOWLIST)
            try:
                argv = build_local_sandbox_exec_command(
                    sandbox_binary=self._settings.SANDBOX_BINARY,
                    session_id=context.session_id,
                    rootfs_path=context.rootfs_path,
                    bin_dir=shared_bin,
                    cwd=normalized_cwd,
                    command=resolved_command,
                    env=env_overrides,
                    memory_limit_bytes=self._settings.SESSION_MEMORY_MAX,
                    pids_max=self._settings.SESSION_PIDS_MAX,
                    cpu_quota_pct=self._settings.SESSION_CPU_QUOTA_PCT,
                    rlimit_as=self._settings.SESSION_RLIMIT_AS,
                    rlimit_fsize=self._settings.SESSION_RLIMIT_FSIZE,
                    rlimit_cpu_sec=self._settings.SESSION_RLIMIT_CPU_SEC,
                    rlimit_nofile=self._settings.SESSION_RLIMIT_NOFILE,
                    rlimit_nproc=self._settings.SESSION_RLIMIT_NPROC,
                    env_file_path=str(exec_dir / "env"),
                    exec_allowlist_path=str(exec_dir / "exec.allow"),
                    network_enabled=self._settings.NETWORK_ENABLED,
                    exec_allowlist=self._settings.EXEC_ALLOWLIST,
                )
            except Exception:
                _cleanup_paths((exec_dir,))
                raise
            process_cwd = None
            process_env_arg = None
            cleanup_paths = (exec_dir,)
        elif self._settings.RUNTIME_MODE == RuntimeMode.BWRAP:
            argv = build_bwrap_exec_command(
                systemd_run_binary=self._settings.SYSTEMD_RUN_BINARY,
                bwrap_binary=self._settings.BWRAP_BINARY,
                rootfs_path=context.rootfs_path,
                bin_dir=self._settings.SHARED_BIN_DIR,
                cwd=normalized_cwd,
                command=list(command),
                env=env_overrides,
                network_enabled=self._settings.NETWORK_ENABLED,
                memory_limit_bytes=self._settings.MEMORY_LIMIT_BYTES,
                max_tasks=self._settings.MAX_TASKS,
                cpu_quota_pct=self._settings.CPU_QUOTA_PCT,
            )
            process_cwd = None
            process_env_arg = None
            cleanup_paths = ()
        else:
            raise InternalServerErrorException(
                "unsupported runtime mode",
                details={"runtimeMode": str(self._settings.RUNTIME_MODE)},
            )

        logger.debug(
            "runtime.prepare_process mode=%s session_id=%s cwd=%s env_keys=%s command=%s",
            self._settings.RUNTIME_MODE.value,
            context.session_id,
            normalized_cwd,
            sorted(env_overrides.keys()),
            _redact_argv(argv),
            extra={
                "event.category": "processing",
                "event.action": "receive",
                "app.operation": "RuntimeAdapter._prepare_process",
                "event.outcome": "unknown",
                "app.status": "accepted",
                "diagnostics": _process_diagnostics(
                    runtime_mode=self._settings.RUNTIME_MODE.value,
                    context=context,
                    normalized_cwd=normalized_cwd,
                    argv=argv,
                    process_cwd=process_cwd,
                    env_keys=sorted(env_overrides.keys()),
                    wrapper_name=Path(self._settings.SANDBOX_BINARY).name
                    if self._settings.RUNTIME_MODE == RuntimeMode.LOCAL
                    else None,
                ),
            },
        )
        return _RuntimeProcessSpec(
            argv=argv,
            process_cwd=process_cwd,
            process_env=process_env_arg,
            normalized_cwd=normalized_cwd,
            diagnostics=_process_diagnostics(
                runtime_mode=self._settings.RUNTIME_MODE.value,
                context=context,
                normalized_cwd=normalized_cwd,
                argv=argv,
                process_cwd=process_cwd,
                env_keys=sorted(env_overrides.keys()),
                wrapper_name=Path(self._settings.SANDBOX_BINARY).name
                if self._settings.RUNTIME_MODE == RuntimeMode.LOCAL
                else None,
            ),
            cleanup_paths=cleanup_paths,
        )

    async def _run_exec(
        self,
        *,
        process: _RuntimeProcessSpec,
        stdin: str | bytes | None,
        timeout_sec: int,
        output_limit_bytes: int,
        rootfs_path: str,
    ) -> ExecResult:
        stdin_bytes = _encode_stdin(stdin)
        started_at = time.perf_counter()
        proc = await self._spawn_process(
            process=process,
            stdin=stdin_bytes,
        )

        watchdog = _QuotaWatchdog(
            rootfs_path=rootfs_path,
            max_total_bytes=self._settings.MAX_TOTAL_BYTES,
            pids_max=self._settings.SESSION_PIDS_MAX,
            interval_sec=self._settings.SESSION_FILE_WATCHDOG_INTERVAL_SEC,
        )
        watchdog_task = asyncio.create_task(watchdog.run(proc, process.diagnostics))
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(stdin_bytes),
                    timeout=timeout_sec,
                )
                kill_reason = watchdog.kill_reason
                result = _build_exec_result(
                    exit_code=proc.returncode or 0,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    output_limit_bytes=output_limit_bytes,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    kill_reason=kill_reason,
                )
                if result.exit_code != 0:
                    logger.warning(
                        "runtime.exec_nonzero_exit",
                        extra={
                            "event.category": "processing",
                            "event.action": "complete",
                            "app.operation": "RuntimeAdapter._run_exec",
                            "event.outcome": "success",
                            "app.status": "processed",
                            "diagnostics": _exec_result_diagnostics(
                                process.diagnostics,
                                result=result,
                                stdout_bytes=stdout_bytes,
                                stderr_bytes=stderr_bytes,
                                phase="exec-exit",
                            ),
                        },
                    )
                return result
            except TimeoutError:
                await self._terminate_process(proc)
                stdout_bytes, stderr_bytes = await proc.communicate()
                kill_reason = watchdog.kill_reason
                result = _build_exec_result(
                    exit_code=124,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    output_limit_bytes=output_limit_bytes,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    kill_reason=kill_reason,
                )
                timeout_stderr = result.stderr or "Command timed out"
                timeout_result = replace(result, exit_code=124, stderr=timeout_stderr, timed_out=True)
                logger.warning(
                    "runtime.exec_timeout",
                    extra={
                        "event.category": "processing",
                        "event.action": "complete",
                        "app.operation": "RuntimeAdapter._run_exec",
                        "event.outcome": "success",
                        "app.status": "processed",
                        "diagnostics": _exec_result_diagnostics(
                            process.diagnostics,
                            result=timeout_result,
                            stdout_bytes=stdout_bytes,
                            stderr_bytes=stderr_bytes,
                            phase="exec-timeout",
                            timeout_sec=timeout_sec,
                        ),
                    },
                )
                return timeout_result
        finally:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watchdog_task

    async def _spawn_process(
        self,
        *,
        process: _RuntimeProcessSpec,
        stdin: bytes | None,
    ) -> asyncio.subprocess.Process:
        op = "RuntimeAdapter._spawn_process"
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *process.argv,
                cwd=process.process_cwd,
                env=process.process_env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            diagnostics = _spawn_failure_diagnostics(process.diagnostics, exc=exc, phase="spawn")
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "runtime.spawn_not_found",
                exc_info=True,
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    "diagnostics": diagnostics,
                },
            )
            raise InternalServerErrorException(
                "runtime binary was not found",
                details=_runtime_error_details(diagnostics),
                additional=str(exc),
            ) from exc
        except OSError as exc:
            diagnostics = _spawn_failure_diagnostics(process.diagnostics, exc=exc, phase="spawn")
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "runtime.spawn_failed",
                exc_info=True,
                extra={
                    "event.category": "processing",
                    "event.action": "failure",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    "diagnostics": diagnostics,
                },
            )
            raise InternalServerErrorException(
                "failed to start runtime process",
                details=_runtime_error_details(diagnostics),
                additional=str(exc),
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            f"{op} success",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": op,
                "event.outcome": "success",
                "app.status": "processed",
                "app.duration_ms": elapsed_ms,
                "diagnostics": {**process.diagnostics, "phase": "spawn-success"},
            },
        )
        return proc

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return

        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._settings.SIGTERM_GRACE_SEC)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _write_stdin(self, proc: asyncio.subprocess.Process, stdin: bytes | None) -> None:
        if stdin is None or proc.stdin is None:
            return
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.stdin.wait_closed()

    async def _pump_stream(
        self,
        stream: asyncio.StreamReader | None,
        kind: str,
        queue: asyncio.Queue[tuple[str, str] | None],
    ) -> None:
        if stream is None:
            await queue.put(None)
            return

        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                await queue.put((kind, chunk.decode("utf-8", errors="replace")))
        finally:
            await queue.put(None)

    async def _check_systemd_user_scope(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._settings.SYSTEMD_RUN_BINARY,
                "--user",
                "--scope",
                "--quiet",
                "true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, f"runtime binary is unavailable: {self._settings.SYSTEMD_RUN_BINARY}"
        except OSError as exc:
            return False, f"failed to start runtime preflight: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            await self._terminate_process(proc)
            return False, "systemd user scope preflight timed out"

        if proc.returncode == 0:
            return True, "ok"

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
        return False, f"systemd user scope preflight failed: {detail}"

    def _availability_result(self, available: bool, detail: str) -> tuple[bool, str]:
        log_method = logger.info if available else logger.warning
        log_method(
            "runtime.availability_check",
            extra={
                "event.category": "processing",
                "event.action": "complete",
                "app.operation": "RuntimeAdapter._availability_result",
                "event.outcome": "success",
                "app.status": "processed",
                "diagnostics": {
                    "phase": "runtime-availability-check",
                    "runtimeMode": self._settings.RUNTIME_MODE.value,
                    "available": available,
                    "detail": detail,
                    "rootfsTemplateDir": self._settings.ROOTFS_TEMPLATE_DIR,
                    "sandboxBinary": self._settings.SANDBOX_BINARY,
                    "bwrapBinary": self._settings.BWRAP_BINARY,
                    "systemdRunBinary": self._settings.SYSTEMD_RUN_BINARY,
                },
            },
        )
        return available, detail


def _encode_stdin(stdin: str | bytes | None) -> bytes | None:
    if stdin is None:
        return None
    if isinstance(stdin, bytes):
        return stdin
    return stdin.encode("utf-8")


def _build_exec_result(
    *,
    exit_code: int,
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    output_limit_bytes: int,
    duration_ms: int,
    kill_reason: str | None = None,
) -> ExecResult:
    stdout_slice = stdout_bytes[:output_limit_bytes]
    stderr_slice = stderr_bytes[:output_limit_bytes]
    return ExecResult(
        exit_code=exit_code,
        stdout=stdout_slice.decode("utf-8", errors="replace"),
        stderr=stderr_slice.decode("utf-8", errors="replace"),
        duration_ms=duration_ms,
        stdout_truncated=len(stdout_bytes) > output_limit_bytes,
        stderr_truncated=len(stderr_bytes) > output_limit_bytes,
        kill_reason=kill_reason,
    )


class _QuotaWatchdog:
    def __init__(
        self,
        *,
        rootfs_path: str,
        max_total_bytes: int,
        pids_max: int,
        interval_sec: float,
    ) -> None:
        self._rootfs_path = Path(rootfs_path)
        self._max_total_bytes = max_total_bytes
        self._pids_max = pids_max
        self._interval_sec = interval_sec
        self.kill_reason: str | None = None
        self.observed_bytes = 0
        self.observed_pids = 0

    @property
    def triggered(self) -> bool:
        return self.kill_reason is not None

    async def run(self, proc: asyncio.subprocess.Process, diagnostics: dict[str, Any]) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_sec)
                if proc.returncode is not None:
                    return
                try:
                    total = await asyncio.to_thread(calculate_total_bytes, self._rootfs_path)
                except OSError:
                    logger.warning(
                        "runtime.quota_watchdog_scan_failed",
                        exc_info=True,
                        extra={
                            "event.category": "processing",
                            "event.action": "failure",
                            "app.operation": "RuntimeAdapter._QuotaWatchdog.run",
                            "event.outcome": "failure",
                            "app.status": "failed",
                            "diagnostics": {
                                **diagnostics,
                                "phase": "quota-watchdog-scan-failed",
                            },
                        },
                    )
                else:
                    self.observed_bytes = total
                    if total > self._max_total_bytes:
                        self.kill_reason = "quota"
                        logger.warning(
                            "runtime.quota_watchdog_kill",
                            extra={
                                "event.category": "processing",
                                "event.action": "failure",
                                "app.operation": "RuntimeAdapter._QuotaWatchdog.run",
                                "event.outcome": "failure",
                                "app.status": "failed",
                                "diagnostics": {
                                    **diagnostics,
                                    "phase": "quota-watchdog-kill",
                                    "observedBytes": total,
                                    "maxTotalBytes": self._max_total_bytes,
                                },
                            },
                        )
                        self._kill(proc)
                        return

                if self._pids_max > 0:
                    try:
                        pids_count = await asyncio.to_thread(_count_pgid_processes, proc.pid)
                    except OSError:
                        logger.warning(
                            "runtime.pids_watchdog_scan_failed",
                            exc_info=True,
                            extra={
                                "event.category": "processing",
                                "event.action": "failure",
                                "app.operation": "RuntimeAdapter._QuotaWatchdog.run",
                                "event.outcome": "failure",
                                "app.status": "failed",
                                "diagnostics": {
                                    **diagnostics,
                                    "phase": "pids-watchdog-scan-failed",
                                },
                            },
                        )
                    else:
                        self.observed_pids = pids_count
                        if pids_count > self._pids_max:
                            self.kill_reason = "pids"
                            logger.warning(
                                "runtime.pids_watchdog_kill",
                                extra={
                                    "event.category": "processing",
                                    "event.action": "failure",
                                    "app.operation": "RuntimeAdapter._QuotaWatchdog.run",
                                    "event.outcome": "failure",
                                    "app.status": "failed",
                                    "diagnostics": {
                                        **diagnostics,
                                        "phase": "pids-watchdog-kill",
                                        "observedPids": pids_count,
                                        "maxPids": self._pids_max,
                                    },
                                },
                            )
                            self._kill(proc)
                            return
        except asyncio.CancelledError:
            return

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            return


def _count_pgid_processes(pgid: int) -> int:
    # /proc/<pid>/stat layout: pid (comm) state ppid pgrp ...
    # comm may contain spaces and parens, so we anchor on the last ')'.
    count = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                data = handle.read()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        except OSError:
            continue
        rparen = data.rfind(b")")
        if rparen < 0:
            continue
        tail = data[rparen + 1 :].split()
        if len(tail) < 3:
            continue
        try:
            if int(tail[2]) == pgid:
                count += 1
        except ValueError:
            continue
    return count


class _TailTextBuffer:
    def __init__(self, limit_chars: int = _OUTPUT_TAIL_BYTES) -> None:
        self._limit_chars = limit_chars
        self._value = ""

    @property
    def value(self) -> str:
        return self._value

    def append(self, data: str) -> None:
        self._value = (self._value + data)[-self._limit_chars :]


def _process_diagnostics(
    *,
    runtime_mode: str,
    context: RuntimeContext,
    normalized_cwd: str,
    argv: list[str],
    process_cwd: str | None,
    env_keys: list[str],
    wrapper_name: str | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "runtimeMode": runtime_mode,
        "sessionId": context.session_id,
        "rootfsPath": context.rootfs_path,
        "cwd": normalized_cwd,
        "processCwd": process_cwd,
        "binary": argv[0] if argv else None,
        "argv": _redact_argv(argv),
        "envKeys": env_keys,
    }
    if wrapper_name is not None:
        diagnostics["wrapper"] = wrapper_name
    return diagnostics


def _redact_argv(argv: list[str]) -> list[Any]:
    return [redact_log_value(item) for item in argv]


def _decode_tail(value: bytes) -> str:
    if not value:
        return ""
    return value[-_OUTPUT_TAIL_BYTES:].decode("utf-8", errors="replace")


def _exec_result_diagnostics(
    diagnostics: dict[str, Any],
    *,
    result: ExecResult,
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    phase: str,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    payload = {
        **diagnostics,
        "phase": phase,
        "exitCode": result.exit_code,
        "durationMs": result.duration_ms,
        "timedOut": result.timed_out,
        "stdoutBytes": len(stdout_bytes),
        "stderrBytes": len(stderr_bytes),
        "stdoutTruncated": result.stdout_truncated,
        "stderrTruncated": result.stderr_truncated,
        "stdoutTail": _decode_tail(stdout_bytes),
        "stderrTail": _decode_tail(stderr_bytes) or result.stderr,
    }
    if timeout_sec is not None:
        payload["timeoutSec"] = timeout_sec
    return payload


def _spawn_failure_diagnostics(
    diagnostics: dict[str, Any],
    *,
    exc: OSError,
    phase: str,
) -> dict[str, Any]:
    return {
        **diagnostics,
        "phase": phase,
        "errorType": type(exc).__name__,
        "errno": exc.errno,
        "errorMessage": str(exc),
    }


def _runtime_error_details(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": diagnostics.get("phase"),
        "runtimeMode": diagnostics.get("runtimeMode"),
        "sessionId": diagnostics.get("sessionId"),
        "cwd": diagnostics.get("cwd"),
        "binary": diagnostics.get("binary"),
        "errno": diagnostics.get("errno"),
        "errorType": diagnostics.get("errorType"),
    }


async def _gather_safely(*tasks: asyncio.Task[None]) -> None:
    for task in tasks:
        if not task.done():
            await task


def _cleanup_paths(paths: tuple[Path, ...]) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def _binary_exists(binary: str) -> bool:
    return Path(binary).exists() if "/" in binary else which(binary) is not None


def _binary_is_executable(binary: str) -> bool:
    resolved = Path(binary) if "/" in binary else Path(which(binary) or "")
    return resolved.is_file() and os.access(resolved, os.X_OK)


def _cleanup_local_cgroup(rootfs_path: str) -> None:
    session_id = Path(rootfs_path).name
    sanitized = _CGROUP_SAFE_CHARS_RE.sub("_", session_id)
    if not sanitized:
        return

    cgroup_path = CGROUP_ROOT / f"ws-{sanitized}"
    try:
        cgroup_path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTEMPTY, errno.EBUSY}:
            logger.debug(
                "runtime.cgroup_cleanup_skipped path=%s errno=%s",
                cgroup_path,
                exc.errno,
            )
            return
        logger.warning(
            "runtime.cgroup_cleanup_failed path=%s errno=%s",
            cgroup_path,
            exc.errno,
        )


def _wrapper_warning_steps(stderr: str) -> set[str]:
    steps: set[str] = set()
    for line in stderr.splitlines():
        match = _WRAPPER_WARNING_RE.match(line)
        if match is not None:
            steps.add(match.group("step"))
    return steps
