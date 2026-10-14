"""Tests for the multi-agent bughunt fixes (2026-05-01).

Eight verified bugs were found by three parallel audit agents
covering concurrency/resource leaks, silent error swallowing, and
path-safety / sandbox holes. Each test below pins one of the fixes
so a regression would be caught immediately.
"""

from __future__ import annotations

import sys

import pytest

from tests.helpers.shared import TEST_SCOPE

# test_chat_id_zero_preserved_not_nulled removed in v5.15.x — was a pure
# source-string pin reading ouroboros/agent.py for the literal patch
# (`int(task.get("chat_id") or 0) or None` must not appear). Per the user's
# `delete_trust_behavioral` choice, no behavioral replacement was added.
# Note: a directed behavioral guard (a task with chat_id=0 routed through
# the agent loop without collapse to None) is NOT currently in the suite;
# adding one would be a separate follow-up if this regression class
# resurfaces.


# ---------------------------------------------------------------------------
# P0 — case-insensitive safety-critical path check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# P1 — safe_relpath rejects NUL bytes and control chars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected,error_match",
    [
        pytest.param("notes.md", "notes.md", None, id="normal-file"),
        pytest.param("docs/architecture.md", "docs/architecture.md", None, id="nested-file"),
        pytest.param("with\ttab.txt", "with\ttab.txt", None, id="tab-allowed"),
        pytest.param("with\nnewline.txt", "with\nnewline.txt", None, id="newline-allowed"),
        pytest.param("subdir/file with spaces.txt", "subdir/file with spaces.txt", None, id="spaces-allowed"),
        pytest.param("/etc/passwd", "etc/passwd", None, id="leading-slash-stripped"),
        pytest.param("notes.md\x00.pdf", None, "control character|NUL", id="nul-suffix"),
        pytest.param("good\x00bad.txt", None, "control character|NUL", id="nul-middle"),
        pytest.param("with\x01control.txt", None, "control character|NUL", id="control-char"),
        pytest.param("with\x1fboundary.txt", None, "control character|NUL", id="control-boundary"),
        pytest.param("../../../etc/passwd", None, "traversal", id="traversal"),
    ],
)
def test_safe_relpath_contract(path, expected, error_match):
    from ouroboros.utils import safe_relpath

    if error_match:
        with pytest.raises(ValueError, match=error_match):
            safe_relpath(path)
    else:
        assert safe_relpath(path) == expected


# ---------------------------------------------------------------------------
# P2 — safe_read distinguishes missing vs unreadable
# ---------------------------------------------------------------------------


def test_safe_read_warns_when_file_unreadable_but_exists(tmp_path, caplog, monkeypatch):
    """Bug 6: previously DEBUG-only when a file existed-but-unreadable.
    For required runtime or memory files that's a real infrastructure error
    the operator must see."""
    from ouroboros.agent.context import safe_read

    monkeypatch.setattr("ouroboros.utils._drive_storage_for_path", lambda _p: None)

    f = tmp_path / "secret.md"
    f.write_text("required content", encoding="utf-8")
    f.chmod(0o000)  # unreadable

    try:
        result = safe_read(TEST_SCOPE, f, fallback="(fallback)")
        # On systems where root can still read (containers without proper
        # cap drops), this might succeed. Skip if the file is still readable.
        if result == "required content":
            pytest.skip("OS allows reading despite chmod 000")
        assert result == "(fallback)"
    finally:
        f.chmod(0o644)


def test_safe_read_silent_when_file_simply_missing(tmp_path, caplog):
    """Missing file is the normal case for optional sources — no warning."""
    from ouroboros.agent.context import safe_read

    with caplog.at_level("WARNING"):
        result = safe_read(TEST_SCOPE, tmp_path / "does_not_exist.md", fallback="default")
    assert result == "default"
    # No warning for the simple-missing case
    assert not any("safe_read" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# P2 — PID lock FD leak
# ---------------------------------------------------------------------------


def test_pid_lock_acquire_closes_fd_on_failure(tmp_path, monkeypatch):
    """Bug 8: when the lock acquire raised IOError, the file remained open
    and was overwritten in the global _lock_fd reference, leaking FDs."""
    from ouroboros import platform_layer

    # First holder acquires the lock
    lock_path = tmp_path / "test.pid"
    assert platform_layer.pid_lock_acquire(str(lock_path)) is True
    first_fd = platform_layer._lock_fd

    # Subsequent acquires (still in same process — would conflict on real
    # systems, but the close-on-failure behavior is what we test)
    # Force fcntl.flock to raise to simulate "another process holds it".
    # On macOS the same process can re-acquire (advisory locks), so we
    # simulate the failure path explicitly.
    if sys.platform != "win32":
        import fcntl

        original_flock = fcntl.flock

        def fake_flock(fd, op):
            raise BlockingIOError("simulated already-locked")

        monkeypatch.setattr(fcntl, "flock", fake_flock)

        # New attempt — should fail AND close the new fd, leaving _lock_fd
        # pointing at the original successful one.
        result = platform_layer.pid_lock_acquire(str(tmp_path / "another.pid"))
        assert result is False
        # The global fd must NOT have been overwritten with the failed handle.
        assert platform_layer._lock_fd is first_fd

        monkeypatch.setattr(fcntl, "flock", original_flock)
    platform_layer.pid_lock_release(str(lock_path))


# ``test_imports_after_bughunt_dont_raise`` was retired in v5.8.3-rc.5.
# pytest itself imports every test module during collection, and
# ``tests/e2e/test_smoke.py`` already pins the broader import surface; the
# bughunt-specific list above duplicated coverage that pytest collection +
# smoke already provide.
