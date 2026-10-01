"""Tests for the discoverable calculator tool."""

from __future__ import annotations

import pathlib

import pytest

from tests._shared import make_tool_env, run_tool


def _calc(env, expression: str) -> str:
    catalog, ctx = env
    return run_tool(catalog, ctx, "calculator", {"expression": expression})


@pytest.fixture
def calculator_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OUROBOROS_ENABLED_TOOLS", "calculator")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    return make_tool_env(tmp_path / "repo", tmp_path / "data")


def test_arithmetic_still_works(calculator_env) -> None:
    assert _calc(calculator_env, "2 + 2 * 3") == "8"
    assert _calc(calculator_env, "(10 + 5) / 3") == "5.0"


@pytest.mark.parametrize(
    "expression,expected",
    [
        pytest.param("min(71.63, 128.06, 174.60)", "71.63", id="min-many"),
        pytest.param("max(1, 2)", "2", id="max-two"),
        pytest.param("max(min(1, 2), min(3, 4))", "3", id="nested"),
        pytest.param("min(10, 20) + max(1, 2)", "12", id="combined-arithmetic"),
    ],
)
def test_min_max_expressions(calculator_env, expression: str, expected: str) -> None:
    assert _calc(calculator_env, expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "sum(1, 2)",
        "min()",
        "max(1)",
        "min(1)",
        "min('a', 2)",
        "math.min(1, 2)",
        "min(1, 2, keyword=3)",
    ],
)
def test_rejects_unsupported_calls(calculator_env, expression: str) -> None:
    result = _calc(calculator_env, expression)
    assert result.startswith("⚠️ CALCULATOR_ERROR:")
