from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from tests.helpers.dialogs_cursor import sql_dialogs_cursor_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.sql_faults import fail_sql
from transport.dialogs.cursor import DialogsCursor


def test_cursor_loads_once_from_its_repository() -> None:
    repository = Mock(wraps=sql_dialogs_cursor_repository())
    repository.save_cursor(17)
    cursor = DialogsCursor(repository)

    assert cursor.value == 17
    repository.save_cursor(23)
    assert cursor.value == 17
    repository.load_cursor.assert_called_once_with()
    assert sql_dialogs_cursor_repository().load_cursor() == 23


def test_commit_persists_before_updating_the_cached_cursor() -> None:
    repository = Mock(wraps=sql_dialogs_cursor_repository())
    sql_dialogs_cursor_repository().save_cursor(17)
    cursor = DialogsCursor(repository)

    cursor.commit("23")

    assert cursor.value == sql_dialogs_cursor_repository().load_cursor() == 23
    repository.save_cursor.assert_called_once_with(23)
    repository.load_cursor.assert_not_called()


@parametrize_from_dict(
    {
        "none": {"value": None},
        "not a number": {"value": "invalid"},
        "unsupported type": {"value": {}},
    }
)
def test_invalid_commit_does_not_write(value) -> None:
    repository = Mock(wraps=sql_dialogs_cursor_repository())
    sql_dialogs_cursor_repository().save_cursor(17)
    cursor = DialogsCursor(repository)

    cursor.commit(value)

    assert cursor.value == 17
    repository.save_cursor.assert_not_called()
    assert sql_dialogs_cursor_repository().load_cursor() == 17


def test_failed_commit_keeps_the_last_saved_cursor_and_can_be_retried() -> None:
    repository = sql_dialogs_cursor_repository()
    repository.save_cursor(17)
    cursor = DialogsCursor(repository)
    assert cursor.value == 17

    with fail_sql("UPDATE owner_runtime_state", OSError("unavailable")):
        with pytest.raises(OSError, match="unavailable"):
            cursor.commit(23)

    assert cursor.value == repository.load_cursor() == 17
    cursor.commit(23)
    assert cursor.value == repository.load_cursor() == 23


def test_failed_read_logs_and_starts_from_zero(caplog) -> None:
    repository = Mock(wraps=sql_dialogs_cursor_repository())
    cursor = DialogsCursor(repository)

    with fail_sql("owner_runtime_state", OSError("unavailable")):
        with caplog.at_level(logging.WARNING, logger="transport.dialogs.cursor"):
            assert cursor.value == 0

    assert "Failed to read the dialogs cursor" in caplog.text
    assert cursor.value == 0
    repository.load_cursor.assert_called_once_with()
