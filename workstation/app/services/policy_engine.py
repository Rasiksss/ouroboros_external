"""Policy engine for exec and filesystem operations."""

from __future__ import annotations

import logging
import shlex
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.utils.exceptions.common import PolicyDeniedException, ValidationErrorException
from app.utils.logging import redact_log_value
from app.utils.paths import normalize_virtual_path

_SHELL_BINARIES = {"sh", "bash", "dash", "zsh"}
_SHELL_COMMAND_FLAGS = {"-c", "-lc"}
logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ExecPolicyDecision:
    cwd: str
    env: dict[str, str]


class PolicyEngine:
    """Evaluate command and path policy decisions."""

    def validate_exec(self, command: list[str], cwd: str, env: dict[str, str]) -> ExecPolicyDecision:
        op = "PolicyEngine.validate_exec"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "guardrails",
                "event.action": "validate",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        extra_diagnostics = {"diagnostics": {}}
        try:
            if not command:
                raise ValidationErrorException("command must not be empty", details={"field": "command"})

            extra_diagnostics["diagnostics"].update(
                {
                    "phase": "policy-exec-validate-start",
                    "command": redact_log_value(command),
                    "cwd": cwd,
                    "envKeys": sorted(env.keys()),
                    "denyCommandTokens": sorted(settings.DENY_COMMAND_TOKENS),
                }
            )
            denied_token = self._find_denied_command_token(command)
            if denied_token is not None:
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "policy-exec-denied-token",
                        "command": redact_log_value(command),
                        "cwd": cwd,
                        "token": denied_token,
                    }
                )
                raise PolicyDeniedException(
                    f"command {denied_token!r} is denied",
                    details={"token": denied_token},
                )

            normalized_cwd = self.validate_file_path(cwd)
            forbidden_env = sorted(set(env) - set(settings.ENV_ALLOWLIST))
            if forbidden_env:
                extra_diagnostics["diagnostics"].update(
                    {
                        "phase": "policy-exec-denied-env",
                        "command": redact_log_value(command),
                        "cwd": normalized_cwd,
                        "forbiddenEnv": forbidden_env,
                        "allowedEnv": sorted(settings.ENV_ALLOWLIST),
                    }
                )
                raise PolicyDeniedException(
                    "environment keys are not allowed",
                    details={"forbiddenEnv": forbidden_env},
                )

            result = ExecPolicyDecision(
                cwd=normalized_cwd,
                env={key: env[key] for key in sorted(env)},
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "guardrails",
                    "event.action": "validate",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "guardrails",
                    "event.action": "validate",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return result

    def validate_file_path(self, path: str) -> str:
        op = "PolicyEngine.validate_file_path"
        started = time.perf_counter()
        logger.debug(
            f"{op} start",
            extra={
                "event.category": "guardrails",
                "event.action": "validate",
                "app.operation": op,
                "event.outcome": "unknown",
                "app.status": "accepted",
            },
        )
        extra_diagnostics = {}
        try:
            normalized = normalize_virtual_path(path)
        except ValidationErrorException as exc:
            extra_diagnostics = {
                "diagnostics": {
                    "phase": "policy-path-denied",
                    "path": path,
                    "errorMessage": str(exc),
                }
            }
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "guardrails",
                    "event.action": "validate",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise PolicyDeniedException(
                "path escapes session rootfs",
                details={"path": path},
            ) from exc
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                f"{op} failed",
                extra={
                    "event.category": "guardrails",
                    "event.action": "validate",
                    "app.operation": op,
                    "event.outcome": "failure",
                    "app.status": "failed",
                    "app.duration_ms": elapsed_ms,
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "error.stack_trace": traceback.format_exc(),
                    **extra_diagnostics,
                },
            )
            raise
        else:
            extra_diagnostics = {
                "diagnostics": {
                    "phase": "policy-path-validate",
                    "path": path,
                    "normalizedPath": normalized,
                }
            }
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                f"{op} success",
                extra={
                    "event.category": "guardrails",
                    "event.action": "validate",
                    "app.operation": op,
                    "event.outcome": "success",
                    "app.status": "processed",
                    "app.duration_ms": elapsed_ms,
                    **extra_diagnostics,
                },
            )
            return normalized

    def _find_denied_command_token(self, command: list[str]) -> str | None:
        denylist = {token.lower() for token in settings.DENY_COMMAND_TOKENS}
        for token in command:
            basename = Path(token).name.lower()
            if basename in denylist:
                return basename
        for token in self._shell_payload_tokens(command):
            if token in denylist:
                return token
        return None

    def _shell_payload_tokens(self, command: list[str]) -> list[str]:
        if len(command) < 3:
            return []
        shell = Path(command[0]).name.lower()
        flag = command[1]
        if shell not in _SHELL_BINARIES or flag not in _SHELL_COMMAND_FLAGS:
            return []
        try:
            parts = shlex.split(command[2], posix=True)
        except ValueError:
            return []
        return [Path(part).name.lower() for part in parts]
