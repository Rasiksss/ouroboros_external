"""Logging formatter diagnostics and redaction tests."""

from __future__ import annotations

import json
import logging
import multiprocessing
import re
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.utils.logging import JsonFormatter, PlainTextFormatter, get_log_config

_LOGGING_JSON = Path(__file__).resolve().parents[2] / "logging.json"


def _append_lines(path: str, worker_id: int, count: int, line_size: int) -> None:
    """Module-level so multiprocessing can pickle it as a Process target."""
    handler = logging.FileHandler(path, mode="a")
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for i in range(count):
            marker = f"W{worker_id}-{i}-"
            payload = marker + "X" * (line_size - len(marker))
            handler.emit(
                logging.LogRecord(
                    name="workstation.test",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=i,
                    msg=payload,
                    args=(),
                    exc_info=None,
                )
            )
    finally:
        handler.close()


def test_json_formatter_includes_redacted_extra_diagnostics() -> None:
    record = logging.LogRecord(
        name="workstation.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="runtime failed",
        args=(),
        exc_info=None,
    )
    record.diagnostics = {
        "phase": "spawn",
        "token": "super-secret",
        "denyCommandTokens": ["sudo", "docker"],
        "argv": ["curl", "-H", "Authorization: Bearer abc123"],
        "nested": {"password": "open-sesame", "safe": "token=abc123"},
    }

    payload = json.loads(JsonFormatter().format(record))

    assert payload["body"] == "runtime failed"
    assert payload["diagnostics"]["phase"] == "spawn"
    assert payload["diagnostics"]["token"] == "[redacted]"
    assert payload["diagnostics"]["denyCommandTokens"] == ["sudo", "docker"]
    assert payload["diagnostics"]["argv"] == ["curl", "-H", "Authorization: Bearer [redacted]"]
    assert payload["diagnostics"]["nested"]["password"] == "[redacted]"
    assert payload["diagnostics"]["nested"]["safe"] == "token=[redacted]"


def test_plain_formatter_includes_extra_diagnostics() -> None:
    record = logging.LogRecord(
        name="workstation.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=20,
        msg="runtime warning",
        args=(),
        exc_info=None,
    )
    record.diagnostics = {"phase": "exec-exit", "exitCode": 2}

    message = PlainTextFormatter().format(record)

    assert "runtime warning" in message
    assert '"phase": "exec-exit"' in message
    assert '"exitCode": 2' in message


def test_json_formatter_redacts_secrets_in_message_body() -> None:
    record = logging.LogRecord(
        name="workstation.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="login attempt with token=supersecret123",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert "supersecret123" not in payload["body"]
    assert payload["body"] == "login attempt with token=[redacted]"


def test_plain_formatter_redacts_secrets_in_message() -> None:
    record = logging.LogRecord(
        name="workstation.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="login attempt with token=supersecret123",
        args=(),
        exc_info=None,
    )

    message = PlainTextFormatter().format(record)

    assert "supersecret123" not in message
    assert "token=[redacted]" in message


def test_json_formatter_redacts_secrets_in_exception_text() -> None:
    try:
        raise ValueError("failed with password=hunter2")
    except ValueError:
        record = logging.LogRecord(
            name="workstation.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=10,
            msg="request failed",
            args=(),
            exc_info=True,
        )
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "hunter2" not in payload["exc_info"]
    assert "password=[redacted]" in payload["exc_info"]


def test_get_log_config_wires_fixed_filename_in_append_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_LOG_DIR", str(tmp_path))

    loaded = get_log_config(_LOGGING_JSON)

    file_handler = loaded["handlers"]["file"]
    assert file_handler["class"] == "logging.FileHandler"
    assert file_handler["filename"] == str(tmp_path / "app.log")
    assert file_handler["mode"] == "a"
    assert "file" in loaded["root"]["handlers"]
    assert "file" in loaded["loggers"]["app"]["handlers"]


def test_get_log_config_disables_file_handler_when_dir_unwritable(tmp_path, monkeypatch) -> None:
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("i am a file, not a directory")
    monkeypatch.setattr(settings, "APP_LOG_DIR", str(blocking_file / "sub"))

    loaded = get_log_config(_LOGGING_JSON)

    assert "file" not in loaded["handlers"]
    assert "file" not in loaded["root"]["handlers"]
    assert "file" not in loaded["loggers"]["app"]["handlers"]


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_get_log_config_filename_is_fixed_across_worker_count(tmp_path, monkeypatch, workers) -> None:
    """Every APP_WORKERS process must resolve to the same path.

    fluentbit tails one fixed file; get_log_config must never bake in
    anything process-specific (e.g. a PID), since APP_WORKERS > 1 means
    uvicorn re-runs dictConfig once per worker process using this same dict.
    """
    monkeypatch.setattr(settings, "APP_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "APP_WORKERS", workers)

    loaded = get_log_config(_LOGGING_JSON)

    assert loaded["handlers"]["file"]["filename"] == str(tmp_path / "app.log")


def test_concurrent_processes_append_to_shared_file_without_interleaving(tmp_path) -> None:
    """Multiple processes opening the same path in append mode must not corrupt lines.

    Simulates APP_WORKERS > 1 all appending to the one fixed app.log: each
    worker writes many same-sized lines, then every single line in the
    resulting file must come back intact and attributable to exactly one
    worker/index — proving POSIX's per-write() atomicity on a shared,
    O_APPEND-opened regular file holds at this line size, with no in-app
    rotation touching the file mid-write.
    """
    target = tmp_path / "app.log"
    worker_count, lines_per_worker, line_size = 4, 50, 2048

    processes = [
        multiprocessing.Process(target=_append_lines, args=(str(target), worker_id, lines_per_worker, line_size))
        for worker_id in range(worker_count)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    lines = target.read_text().splitlines()
    assert len(lines) == worker_count * lines_per_worker

    seen: set[tuple[int, int]] = set()
    for line in lines:
        match = re.fullmatch(r"W(\d+)-(\d+)-X+", line)
        assert match, f"corrupted or interleaved line: {line!r}"
        assert len(line) == line_size
        seen.add((int(match.group(1)), int(match.group(2))))
    assert len(seen) == worker_count * lines_per_worker
