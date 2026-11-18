"""Checks for the vendored ws-sandbox artifact copied into Docker images."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_BINARY = REPO_ROOT / "vendor" / "bin" / "ws-sandbox"


def test_vendor_ws_sandbox_contains_current_cli_flags() -> None:
    payload = VENDOR_BINARY.read_bytes()

    assert b"--bin-dir" in payload
    assert b"--rlimit-nproc" in payload
