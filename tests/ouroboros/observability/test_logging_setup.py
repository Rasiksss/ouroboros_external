from __future__ import annotations

import json
import logging
import logging.handlers
import re

import pytest

from ouroboros.observability.aef_tracing import trace_scope
from ouroboros.observability.log_context import bind_log_context
from ouroboros.observability.logging_setup import LogContextFilter, configure_logging, next_level_up
from tests.helpers.log_handlers import logging_configuration_restored


def test_configure_logging_json_to_stdout(capsys):
    configure_logging(level="INFO", log_format="json")

    with bind_log_context(task_id="t-1", correlation_id="c-1"):
        logging.getLogger("tests.logging").info("hello")

    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    out_payload = json.loads(out_line)
    assert out_payload["message"] == "hello"
    assert out_payload["task_id"] == "t-1"
    assert out_payload["correlation_id"] == "c-1"


def test_json_record_carries_the_active_trace_id_alongside_the_other_ids(capsys):
    configure_logging(level="INFO", log_format="json")

    with trace_scope("f47ac10b-58cc-4372-a567-0e02b2c3d479"):
        with bind_log_context(task_id="t-1", correlation_id="c-1"):
            logging.getLogger("tests.logging").info("inside an operation")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["trace_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    assert payload["task_id"] == "t-1"
    assert payload["correlation_id"] == "c-1"


def test_json_record_omits_the_trace_id_outside_an_operation(capsys):
    configure_logging(level="INFO", log_format="json")

    logging.getLogger("tests.logging").info("outside an operation")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "trace_id" not in payload


def test_configure_logging_text_format(capsys):
    configure_logging(level="INFO", log_format="text")
    logging.getLogger("tests.logging").warning("plain text")
    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "plain text" in out_line
    assert "[WARNING]" in out_line


@pytest.fixture(autouse=True)
def restore_logger_levels():
    with logging_configuration_restored():
        yield


def _emitted_messages(capsys) -> list[str]:
    out_lines = [line for line in capsys.readouterr().out.strip().splitlines() if line.strip()]
    return [json.loads(line)["message"] for line in out_lines]


@pytest.mark.parametrize(
    ("app_level", "expected"),
    [
        (logging.DEBUG, logging.INFO),
        (logging.INFO, logging.WARNING),
        (logging.WARNING, logging.ERROR),
        (logging.ERROR, logging.CRITICAL),
        (logging.CRITICAL, logging.CRITICAL),
        (25, logging.WARNING),
    ],
)
def test_next_level_up_maps_app_level_to_library_level(app_level, expected):
    assert next_level_up(app_level) == expected


def test_quieted_library_logs_one_level_above_the_app(capsys):
    configure_logging(level="INFO", log_format="json")
    sdk = logging.getLogger("dialog_bot_sdk")

    sdk.info("sdk chatter")
    sdk.warning("sdk problem")

    messages = _emitted_messages(capsys)
    assert "sdk chatter" not in messages
    assert "sdk problem" in messages


def test_critical_app_level_still_lets_library_critical_through(capsys):
    configure_logging(level="CRITICAL", log_format="json")

    logging.getLogger("dialog_bot_sdk").critical("sdk is on fire")

    assert "sdk is on fire" in _emitted_messages(capsys)


@pytest.mark.parametrize(
    "logger_name",
    ["dialog_bot_sdk.updates", "sqlalchemy.engine.Engine", "sqlalchemy.pool.impl.QueuePool"],
)
def test_nested_library_loggers_are_quieted(logger_name, capsys):
    configure_logging(level="INFO", log_format="json")

    logging.getLogger(logger_name).info("nested chatter")
    logging.getLogger(logger_name).warning("nested problem")

    messages = _emitted_messages(capsys)
    assert "nested chatter" not in messages
    assert "nested problem" in messages


def test_library_loggers_created_after_setup_are_quieted(capsys):
    """Effective level is resolved per record, so the hierarchy covers late loggers too."""
    configure_logging(level="INFO", log_format="json")

    late = logging.getLogger("dialog_bot_sdk.created.after.setup")
    late.info("late chatter")
    late.warning("late problem")

    messages = _emitted_messages(capsys)
    assert "late chatter" not in messages
    assert "late problem" in messages


def test_similarly_named_loggers_are_not_quieted(capsys):
    configure_logging(level="INFO", log_format="json")

    logging.getLogger("dialog_bot_sdk_wrapper").info("ours, keep it")
    logging.getLogger("ouroboros.sqlalchemy").info("ours too, keep it")

    messages = _emitted_messages(capsys)
    assert "ours, keep it" in messages
    assert "ours too, keep it" in messages


def test_next_level_up_caps_at_critical():
    assert next_level_up(logging.CRITICAL) == logging.CRITICAL
    assert next_level_up(logging.CRITICAL + 10) == logging.CRITICAL


def test_json_records_are_pure_ascii_so_any_consumer_reads_them(capsys):
    """A record is read where incidents are investigated, not by the process that wrote it."""
    configure_logging(level="INFO", log_format="json")

    logging.getLogger("tests.logging").info("Файл «отчёт.pdf» заблокирован — политика")

    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert out_line.isascii(), "a consumer decoding as anything but UTF-8 would mangle this line"
    assert json.loads(out_line)["message"] == "Файл «отчёт.pdf» заблокирован — политика"


def test_a_handler_configured_inside_a_test_does_not_outlive_it():
    from tests.helpers.log_handlers import dropping_handlers_pinned_to_the_test_stream

    root = logging.getLogger()
    with dropping_handlers_pinned_to_the_test_stream():
        configure_logging(level="INFO", log_format="json")
        assert _context_bound_handlers(root)

    assert not _context_bound_handlers(root)


def test_a_handler_configured_before_the_test_survives_it():
    from tests.helpers.log_handlers import dropping_handlers_pinned_to_the_test_stream

    root = logging.getLogger()
    configure_logging(level="INFO", log_format="json")
    established = list(root.handlers)

    with dropping_handlers_pinned_to_the_test_stream():
        pass

    assert root.handlers == established


def _context_bound_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if any(isinstance(f, LogContextFilter) for f in h.filters)]


def _set_log_settings(**update):
    from ouroboros.storage.settings.store import load_settings, save_settings

    save_settings(load_settings().model_copy(update=update))


def _set_log_dir(tmp_path):
    _set_log_settings(OUROBOROS_LOG_DIR=str(tmp_path))


def test_file_sink_writes_json_lines_to_app_log(tmp_path, capsys):
    _set_log_dir(tmp_path / "logs")
    assert configure_logging(level="INFO", log_format="json", sink="file") == "file"

    with bind_log_context(task_id="t-9", correlation_id="c-9"):
        logging.getLogger("tests.logging").info("to file")

    lines = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["message"] == "to file"
    assert payload["task_id"] == "t-9"
    # fluent-bit's default_json parser expects Time_Key `time` as e.g. 2021-10-25T10:53:02.947Z
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["time"])
    assert capsys.readouterr().out == ""


def test_both_sink_writes_to_stdout_and_file(tmp_path, capsys):
    _set_log_dir(tmp_path)
    configure_logging(level="INFO", log_format="json", sink="both")

    logging.getLogger("tests.logging").info("everywhere")

    file_line = (tmp_path / "app.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(file_line)["message"] == "everywhere"
    assert json.loads(out_line)["message"] == "everywhere"
    # Each handler formats independently; correlation needs one shared stamp.
    assert json.loads(file_line)["time"] == json.loads(out_line)["time"]


def test_unwritable_log_dir_falls_back_to_stdout(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    _set_log_dir(blocker)
    assert configure_logging(level="INFO", log_format="json", sink="file") == "stdout"

    logging.getLogger("tests.logging").info("still visible")

    messages = _emitted_messages(capsys)
    assert "still visible" in messages
    assert any("File log sink unavailable" in message for message in messages)


def test_unopenable_error_log_falls_back_entirely_to_stdout(tmp_path, capsys):
    """A half-attached file sink would ship every line twice; the fallback is all-or-nothing."""
    (tmp_path / "error.log").mkdir()
    _set_log_settings(OUROBOROS_LOG_DIR=str(tmp_path), OUROBOROS_LOG_ERROR_FILE_ENABLED=True)
    configure_logging(level="INFO", log_format="json", sink="file")

    logging.getLogger("tests.logging").info("stdout only")

    messages = _emitted_messages(capsys)
    assert "stdout only" in messages
    assert any("File log sink unavailable" in message for message in messages)
    assert (tmp_path / "app.log").read_text(encoding="utf-8") == ""


def test_fallback_notice_is_a_warning_that_survives_a_raised_log_level(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    _set_log_dir(blocker)
    configure_logging(level="CRITICAL", log_format="json", sink="file")

    notice = next(
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if "File log sink unavailable" in line
    )
    assert notice["level"] == "WARNING"


def test_sink_from_settings_falls_back_when_the_dir_is_unusable(tmp_path, capsys):
    """The multiuser tiers pass no sink argument, so their fallback runs this path."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    _set_log_settings(OUROBOROS_LOG_DIR=str(blocker), OUROBOROS_LOG_SINK="file")

    assert configure_logging(level="INFO", log_format="json") == "stdout"

    logging.getLogger("tests.logging").info("tier still logs")
    messages = _emitted_messages(capsys)
    assert "tier still logs" in messages
    assert any("File log sink unavailable" in message for message in messages)


def test_reconfigure_closes_previous_file_handlers(tmp_path):
    _set_log_dir(tmp_path)
    configure_logging(level="INFO", log_format="json", sink="file")
    file_handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler))

    configure_logging(level="INFO", log_format="json", sink="stdout")

    assert file_handler.stream is None


def test_reconfigure_leaves_foreign_handlers_open(tmp_path):
    """Closing a handler its owner still holds reopens it in that owner's mode."""
    foreign = logging.FileHandler(tmp_path / "foreign.log", encoding="utf-8")
    logging.getLogger().addHandler(foreign)
    try:
        configure_logging(level="INFO", log_format="json", sink="stdout")
        assert foreign.stream is not None
    finally:
        foreign.close()


def test_sink_resolves_from_settings_when_no_argument_is_passed(tmp_path):
    """The multiuser tiers configure logging without a sink argument."""
    _set_log_settings(OUROBOROS_LOG_DIR=str(tmp_path), OUROBOROS_LOG_SINK="file")

    assert configure_logging(level="INFO", log_format="json") == "file"
    assert (tmp_path / "app.log").exists()


def test_text_format_with_file_sink_warns_on_stderr(tmp_path, capsys):
    """The file it would otherwise land in is the one the shipper cannot parse."""
    _set_log_dir(tmp_path)
    configure_logging(level="INFO", log_format="text", sink="file")

    assert "cannot parse" in capsys.readouterr().err
    assert "cannot parse" not in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_debug_level_adds_callsite_fields(capsys):
    configure_logging(level="DEBUG", log_format="json")
    logging.getLogger("tests.logging").info("where am I")
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    # A DEBUG root outlives capsys, and the next asyncio record hits a closed stream.
    configure_logging(level="INFO", log_format="json")

    assert payload["file"].endswith("tests/ouroboros/observability/test_logging_setup.py")
    assert payload["func"] == "test_debug_level_adds_callsite_fields"
    assert isinstance(payload["line"], int)


def test_non_debug_level_omits_callsite_fields(capsys):
    configure_logging(level="INFO", log_format="json")

    logging.getLogger("tests.logging").info("lean record")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "file" not in payload and "line" not in payload and "func" not in payload


def test_error_file_receives_only_errors_and_app_log_keeps_the_full_stream(tmp_path):
    _set_log_settings(OUROBOROS_LOG_DIR=str(tmp_path), OUROBOROS_LOG_ERROR_FILE_ENABLED=True)
    configure_logging(level="INFO", log_format="json", sink="file")

    logging.getLogger("tests.logging").info("routine")
    logging.getLogger("tests.logging").error("broken")

    app_log = (tmp_path / "app.log").read_text(encoding="utf-8")
    error_log = (tmp_path / "error.log").read_text(encoding="utf-8")
    # app.log is what fluent-bit ships, so errors must stay in it too.
    assert "routine" in app_log and "broken" in app_log
    assert "broken" in error_log and "routine" not in error_log


def test_time_rotation_uses_timed_handler(tmp_path):
    _set_log_settings(OUROBOROS_LOG_DIR=str(tmp_path), OUROBOROS_LOG_ROTATION="time")
    configure_logging(level="INFO", log_format="json", sink="file")

    logging.getLogger("tests.logging").info("timed")

    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in handlers)
    assert "timed" in (tmp_path / "app.log").read_text(encoding="utf-8")


class _FullDisk:
    """Stands in for a stream whose volume filled after the handler opened."""

    def write(self, *_args):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        pass


def test_write_failure_moves_logging_to_stdout(tmp_path, capsys):
    _set_log_dir(tmp_path)
    configure_logging(level="INFO", log_format="json", sink="file")
    file_handler = next(h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler))
    file_handler.stream = _FullDisk()

    logging.getLogger("tests.logging").info("disk just filled")
    logging.getLogger("tests.logging").info("and the next one too")

    messages = _emitted_messages(capsys)
    assert any("File log sink failed while writing" in message for message in messages)
    # Neither the record that hit the failure nor the ones after it are lost.
    assert "disk just filled" in messages
    assert "and the next one too" in messages
    assert not any(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers)
