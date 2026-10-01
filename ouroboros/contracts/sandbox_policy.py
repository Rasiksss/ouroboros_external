"""Sandbox execution policy contract.

Frozen types describing subprocess confinement constraints.
No business logic — place behaviour in ouroboros/sandbox/.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class NetworkPolicy(Enum):
    DENY = "deny"  # unshare --net when usable on Linux; else soft scrub of proxy vars
    ALLOW = "allow"


@dataclass(frozen=True)
class SandboxPolicy:
    """Immutable subprocess confinement specification."""

    cwd: Path
    deny_paths: tuple[Path, ...]  # absolute paths; prefix-match against cmd args
    extra_deny_paths: tuple[Path, ...]  # from AppSettings.OUROBOROS_SANDBOX_DENY_PATHS
    deny_name_patterns: tuple[str, ...] = ()  # fnmatch patterns against arg filenames
    timeout_sec: int = 300
    stdout_cap: int = 256 * 1024
    stderr_cap: int = 128 * 1024
    network: NetworkPolicy = NetworkPolicy.DENY
    # Positive filesystem allowlist. Empty = no restriction (soft backend behaviour).
    # Docker backend enforces via bind-mounts; soft backend warns and ignores.
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()


@dataclass
class SandboxResult:
    """Result of a sandboxed subprocess execution."""

    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool
    overflowed: bool
    denied: bool
    denial_reason: str | None
    # Runner collects events; caller (skill_exec) emits via record_event().
    audit_events: list[dict]


# Filename patterns blocked by default for skill subprocesses.
_DEFAULT_DENY_NAME_PATTERNS: tuple[str, ...] = ("*.key", "*.pem", "*.p12", ".env")


def skill_default_policy(cwd: Path, extra_deny: tuple[Path, ...] = ()) -> SandboxPolicy:
    """Confinement policy for manifest-declared skill scripts."""
    return SandboxPolicy(
        cwd=cwd,
        deny_paths=(),
        extra_deny_paths=extra_deny,
        deny_name_patterns=_DEFAULT_DENY_NAME_PATTERNS,
        timeout_sec=300,
        stdout_cap=256 * 1024,
        stderr_cap=128 * 1024,
        network=NetworkPolicy.DENY,
    )


def preflight_default_policy(cwd: Path, extra_deny: tuple[Path, ...] = ()) -> SandboxPolicy:
    """Confinement policy for skill_preflight argv-only validators."""
    return SandboxPolicy(
        cwd=cwd,
        deny_paths=(),
        extra_deny_paths=extra_deny,
        deny_name_patterns=_DEFAULT_DENY_NAME_PATTERNS,
        timeout_sec=30,
        stdout_cap=16 * 1024,
        stderr_cap=16 * 1024,
        network=NetworkPolicy.ALLOW,
    )


class SandboxBackend(Protocol):
    """Contract every sandbox backend must satisfy.

    Implementations: ouroboros/sandbox/soft.py (SoftSandboxBackend),
    ouroboros/sandbox/docker.py (DockerSandboxBackend, P2).
    """

    def run(
        self,
        cmd: list[str],
        policy: SandboxPolicy,
        *,
        env: dict[str, str],
    ) -> SandboxResult: ...


__all__ = [
    "NetworkPolicy",
    "SandboxBackend",
    "SandboxPolicy",
    "SandboxResult",
    "preflight_default_policy",
    "skill_default_policy",
]
