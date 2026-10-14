"""Sandbox execution package.

Public surface:
    SandboxRunner  — dispatches to the backend named by OUROBOROS_SANDBOX_BACKEND.

Backend implementations:
    soft.py    — SoftSandboxBackend (same host, optional unshare --net)
    docker.py  — DockerSandboxBackend (P2, not yet implemented)
"""

from ouroboros.sandbox.runner import SandboxRunner

__all__ = ["SandboxRunner"]
