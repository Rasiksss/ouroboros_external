from __future__ import annotations

import json
import logging

import pytest

from ouroboros.observability.log_context import bind_log_context
from ouroboros.observability.logging_setup import configure_logging, next_level_up


def test_configure_logging_json_to_stdout(capsys):
    configure_logging(level="INFO", log_format="json")

    with bind_log_context(task_id="t-1", correlation_id="c-1"):
        logging.getLogger("tests.logging").info("hello")

    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    out_payload = json.loads(out_line)
    assert out_payload["message"] == "hello"
    assert out_payload["task_id"] == "t-1"
    assert out_payload["correlation_id"] == "c-1"


def test_configure_logging_text_format(capsys):
    configure_logging(level="INFO", log_format="text")
    logging.getLogger("tests.logging").warning("plain text")
    out_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "plain text" in out_line
    assert "[WARNING]" in out_line


@pytest.fixture(autouse=True)
def restore_logger_levels():
    """configure_logging mutates process-wide state, incl. root; snapshot and restore it."""
    snapshot = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    root = logging.getLogger()
    root_level, root_handlers = root.level, list(root.handlers)
    yield
    for name, level in snapshot.items():
        logging.getLogger(name).setLevel(level)
    root.setLevel(root_level)
    root.handlers[:] = root_handlers


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
