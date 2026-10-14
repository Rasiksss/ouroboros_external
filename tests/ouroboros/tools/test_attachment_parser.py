from __future__ import annotations

import asyncio
import logging
import multiprocessing
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.helpers.parametrize import parametrize_from_dict

_PARSER_MODES = {
    "extract": {"mode": "extract"},
    "validate": {"mode": "validate"},
}
_PARSER_OUTCOMES = {
    "successful parser": {"parser_fails": False},
    "failed parser": {"parser_fails": True},
}
_VALIDATION_SURFACES = {
    "synchronous": {"asynchronous": False},
    "asynchronous": {"asynchronous": True},
}
_INTERRUPTIONS = {
    "KeyboardInterrupt": {"interruption": "KeyboardInterrupt"},
    "SystemExit": {"interruption": "SystemExit"},
}


def test_spawn_target_imports_without_application_services():
    from ouroboros.storage.attachment_validation import parser_child

    probe = f"""
import importlib
import sys
for name in ('ouroboros.storage', 'ouroboros.llm', 'new_core', 'server'):
    sys.modules[name] = None
module = importlib.import_module({parser_child.__module__!r})
assert callable(getattr(module, {parser_child.__name__!r}))
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def _stubbornparser_child(connection, path, _mime, mode, _cpu_seconds):
    from ouroboros.platform_layer import install_termination_handlers

    install_termination_handlers(signal.SIG_IGN)
    Path(path).with_suffix(".started").touch()
    if mode == "validate":
        from ouroboros.attachment_parser import ParserResult

        now = time.monotonic()
        connection.send(ParserResult("ok", "", now, now, 0.0, 0.0))
    time.sleep(60)


@parametrize_from_dict(_PARSER_MODES)
def test_parser_reaps_stubborn_child_and_processes_next_file(tmp_path, monkeypatch, isolate_settings, caplog, mode):
    import ouroboros.storage.attachment_validation as attachments
    from ouroboros.storage.attachment_validation import AttachmentError

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    isolate_settings.save_settings(
        {"OUROBOROS_ATTACHMENT_PARSE_TIMEOUT_SEC": 5, "OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC": 0}
    )
    original_children = set(multiprocessing.active_children())
    caplog.set_level(logging.INFO, logger=attachments.__name__)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(attachments, "parser_child", _stubbornparser_child)
            if mode == "extract":
                with pytest.raises(AttachmentError, match="attachment parsing timed out") as error:
                    attachments.run_parser(path, "text/plain", mode=mode)
                assert error.value.retryable
                assert "outcome=timeout startup_ms=unknown import_ms=unknown parse_ms=unknown" in caplog.text
            else:
                assert attachments.run_parser(path, "text/plain", mode=mode) == ""
        assert path.with_suffix(".started").exists()
        assert set(multiprocessing.active_children()) <= original_children
        assert attachments.run_parser(path, "text/plain", mode="extract") == "hello"
    finally:
        for child in set(multiprocessing.active_children()) - original_children:
            child.kill()
            child.join(timeout=5)
            child.close()


@parametrize_from_dict(_PARSER_OUTCOMES)
def test_cancelled_validation_waits_for_worker_and_releases_parser_slot(tmp_path, monkeypatch, caplog, parser_fails):
    from ouroboros.storage import attachment_validation as attachments
    from ouroboros.storage.attachment_validation import AttachmentError

    path = tmp_path / "notes.txt"
    path.write_text("notes")
    started, finish = threading.Event(), threading.Event()

    def parse(*_args):
        started.set()
        assert finish.wait(3)
        if parser_fails:
            raise AttachmentError("invalid_attachment", "broken document")
        return ""

    monkeypatch.setattr(attachments, "_run_parser_process", parse)

    async def exercise():
        pending = asyncio.create_task(attachments.validate_attachment_path_async(path, path.name, "text/plain"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            for _ in range(2):
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
                assert not attachments._PARSER_SLOT.acquire(blocking=False)
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
        assert attachments._PARSER_SLOT.acquire(blocking=False)
        attachments._PARSER_SLOT.release()

    asyncio.run(exercise())
    if parser_fails:
        assert "broken document" in caplog.text


def test_parser_process_truncates_output_with_marker(tmp_path):
    from ouroboros.storage.attachment_validation import run_parser

    path = tmp_path / "notes.txt"
    path.write_text("x" * 100_500, encoding="utf-8")

    result = run_parser(path, "text/plain", mode="extract")

    assert len(result) <= 100_000
    assert result.endswith("\n[TRUNCATED]")


@parametrize_from_dict(_VALIDATION_SURFACES)
def test_validation_records_phases_without_filename_or_content(tmp_path, caplog, asynchronous):
    from ouroboros.storage import attachment_validation as attachments

    path = tmp_path / "private-name.txt"
    path.write_text("private document contents", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=attachments.__name__)

    if asynchronous:
        asyncio.run(attachments.validate_attachment_path_async(path, path.name, "text/plain"))
    else:
        attachments.validate_attachment_path(path, path.name, "text/plain")

    messages = [record.getMessage() for record in caplog.records if record.name == attachments.__name__]
    create_root_task = next(message for message in messages if "event=attachment_parser_admission" in message)
    finished = next(message for message in messages if "event=attachment_parser_finished" in message)
    assert "outcome=acquired" in create_root_task
    fields = dict(field.split("=", 1) for field in finished.split())
    assert fields["outcome"] == "ok"
    phases = [float(fields[key]) for key in ("startup_ms", "import_ms", "parse_ms", "transfer_ms", "cleanup_ms")]
    assert all(phase >= 0 for phase in phases)
    assert abs(sum(phases) - float(fields["total_ms"])) < 0.1
    assert path.name not in caplog.text
    assert "private document contents" not in caplog.text


@parametrize_from_dict(_VALIDATION_SURFACES)
def test_busy_slot_records_admission_timeout_without_starting_child(tmp_path, caplog, isolate_settings, asynchronous):
    from ouroboros.storage import attachment_validation as attachments
    from ouroboros.storage.attachment_validation import AttachmentError

    path = tmp_path / "notes.txt"
    path.write_text("notes", encoding="utf-8")
    isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_SLOT_TIMEOUT_SEC": 0})
    caplog.set_level(logging.INFO, logger=attachments.__name__)

    with attachments._PARSER_SLOT, pytest.raises(AttachmentError) as error:
        if asynchronous:
            asyncio.run(attachments.validate_attachment_path_async(path, path.name, "text/plain"))
        else:
            attachments.validate_attachment_path(path, path.name, "text/plain")

    assert error.value.retryable
    assert "outcome=timeout wait_ms=" in caplog.text
    assert "event=attachment_parser_finished" not in caplog.text


def test_invalid_json_retains_error_and_records_parse_phase(tmp_path, caplog):
    from ouroboros.storage import attachment_validation as attachments
    from ouroboros.storage.attachment_validation import AttachmentError

    path = tmp_path / "broken.json"
    path.write_text("{invalid", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=attachments.__name__)

    with pytest.raises(AttachmentError) as error:
        attachments.validate_attachment_path(path, path.name, "application/json")

    assert (error.value.code, error.value.retryable, error.value.filename) == ("invalid_attachment", False, path.name)
    assert "outcome=invalid startup_ms=" in caplog.text
    assert "parse_ms=unknown" not in caplog.text


def test_worker_keeps_process_memory_and_cpu_limits(monkeypatch):
    import resource

    from ouroboros.attachment_parser import _apply_resource_limits

    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append((kind, limits)))

    _apply_resource_limits(30)

    assert calls == [
        (resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (resource.RLIMIT_CPU, (30, 30)),
    ]


@parametrize_from_dict(_INTERRUPTIONS)
def test_interrupted_worker_closes_pipe_without_reporting_success(interruption):
    probe = f"""
from unittest.mock import patch
from ouroboros.attachment_parser import parser_child
class Connection:
    def send(self, result):
        print('sent', result.status)
    def close(self):
        print('closed')
with patch('pathlib.Path.read_bytes', side_effect={interruption}):
    parser_child(Connection(), 'notes.txt', 'text/plain', 'validate', 30)
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == "closed", result.stderr


def test_spawned_worker_missing_dependency_remains_retryable(tmp_path):
    script = tmp_path / "missing_dependency.py"
    script.write_text(
        """
import sys
from pathlib import Path
if __name__ == '__mp_main__':
    sys.modules['pypdf'] = None
if __name__ == '__main__':
    from ouroboros.storage.attachment_validation import AttachmentError, run_parser
    try:
        run_parser(Path('unused.pdf'), 'application/pdf', mode='validate')
    except AttachmentError as error:
        print(error.code, error.retryable, error.reason)
""",
        encoding="utf-8",
    )

    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=15)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "attachment_parser_unavailable True attachment parser dependency is unavailable: pypdf"
    )
