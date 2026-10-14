from __future__ import annotations

import pytest

from tests.helpers.comparisons import assert_state_subset


def test_state_subset_compares_nested_dictionary() -> None:
    assert_state_subset(
        {"status": "done", "details": {"attempt": 2, "worker": "agent-a"}},
        {"details": {"attempt": 2}},
    )


def test_state_subset_reports_a_mismatched_nested_value() -> None:
    with pytest.raises(AssertionError, match=r"Key 'details\.attempt': 2 != 3"):
        assert_state_subset({"details": {"attempt": 2}}, {"details": {"attempt": 3}})


def test_state_subset_reports_a_missing_key_separately() -> None:
    with pytest.raises(AssertionError, match=r"^Missing key: status$"):
        assert_state_subset({}, {"status": "done"})
