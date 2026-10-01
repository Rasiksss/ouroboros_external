"""SandboxRunner — dispatches to the configured SandboxBackend.

To add a new backend: implement SandboxBackend (contracts/sandbox_policy.py),
add an entry to _BACKENDS. SandboxRunner and all callers remain untouched.
"""

from __future__ import annotations

from ouroboros.app.config import load_settings
from ouroboros.contracts.sandbox_policy import SandboxBackend, SandboxPolicy, SandboxResult
from ouroboros.sandbox.soft import SoftSandboxBackend

_BACKENDS: dict[str, type[SandboxBackend]] = {
    "soft": SoftSandboxBackend,
}


class SandboxRunner:
    """Dispatches run() to the backend named by OUROBOROS_SANDBOX_BACKEND."""

    def run(
        self,
        cmd: list[str],
        policy: SandboxPolicy,
        *,
        env: dict[str, str],
    ) -> SandboxResult:
        """Run cmd with policy enforcement. env is pre-scrubbed by caller."""
        name = str(load_settings().OUROBOROS_SANDBOX_BACKEND or "soft").strip().lower()
        backend_cls = _BACKENDS.get(name)
        if backend_cls is None:
            raise ValueError(f"Unknown sandbox backend: {name!r}. Available: {sorted(_BACKENDS)}")
        return backend_cls().run(cmd, policy, env=env)


__all__ = ["_BACKENDS", "SandboxRunner"]
