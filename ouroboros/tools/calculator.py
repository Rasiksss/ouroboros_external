"""Discoverable arithmetic calculator tool."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

from new_core.tools.context import ToolContext
from ouroboros.tools.spec import spec_from_callable, tool

_DEFAULT_MAX_ABS_NUMBER = 1_000_000
_MAX_EXPRESSION_CHARS = 500

_BINARY_OPERATORS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPERATORS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_ALLOWED_CALLS: dict[str, Callable[..., int | float]] = {
    "min": min,
    "max": max,
}
_MIN_CALL_ARGS = 2


def _max_abs_number() -> int:
    from ouroboros.storage.settings.store import load_settings

    raw = str(load_settings().TOOL_CALCULATOR_MAX_NUMBERS or "").strip()
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ABS_NUMBER
    return parsed if parsed > 0 else _DEFAULT_MAX_ABS_NUMBER


def _literal_value(node: ast.Constant, *, max_abs_number: int) -> int | float:
    value = node.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("only int and float literals are allowed")
    if abs(value) > max_abs_number:
        raise ValueError(f"CALCULATOR_LIMIT: numeric literal {value} exceeds max abs value {max_abs_number}.")
    return value


def _eval_expression(node: ast.AST, *, max_abs_number: int) -> int | float:
    if isinstance(node, ast.Expression):
        return _eval_expression(node.body, max_abs_number=max_abs_number)
    if isinstance(node, ast.Constant):
        return _literal_value(node, max_abs_number=max_abs_number)
    if isinstance(node, ast.BinOp):
        op = _BINARY_OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError("unsupported operator")
        left = _eval_expression(node.left, max_abs_number=max_abs_number)
        right = _eval_expression(node.right, max_abs_number=max_abs_number)
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        unary_op = _UNARY_OPERATORS.get(type(node.op))
        if unary_op is None:
            raise ValueError("unsupported unary operator")
        operand = _eval_expression(node.operand, max_abs_number=max_abs_number)
        return unary_op(operand)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("unsupported function call")
        callee = _ALLOWED_CALLS.get(node.func.id)
        if callee is None:
            raise ValueError("unsupported function call")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        if len(node.args) < _MIN_CALL_ARGS:
            raise ValueError(f"{node.func.id} requires at least {_MIN_CALL_ARGS} arguments")
        values = [_eval_expression(arg, max_abs_number=max_abs_number) for arg in node.args]
        return callee(*values)
    raise ValueError("unsupported syntax")


_CALCULATOR_SCHEMA = {
    "name": "calculator",
    "description": (
        "Evaluate a simple arithmetic expression using literals, "
        "parentheses, unary +/-, operators +, -, *, /, //, %, "
        "and min()/max() over numeric literals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic expression to evaluate.",
            },
        },
        "required": ["expression"],
    },
}


@tool(
    name="calculator",
    schema=_CALCULATOR_SCHEMA,
    category="task",
    surface="discoverable",
    safety_policy="skip",
    parallel=True,
    env_prefix="TOOL_CALCULATOR",
)
def _calculate(_ctx: ToolContext, expression: str = "", **_kwargs: Any) -> str:
    text = str(expression or "").strip()
    if not text:
        return "⚠️ CALCULATOR_ERROR: expression is required."
    if len(text) > _MAX_EXPRESSION_CHARS:
        return f"⚠️ CALCULATOR_ERROR: expression is too long ({len(text)} chars, max {_MAX_EXPRESSION_CHARS})."

    try:
        tree = ast.parse(text, mode="eval")
        result = _eval_expression(tree, max_abs_number=_max_abs_number())
    except SyntaxError as exc:
        return f"⚠️ CALCULATOR_ERROR: {exc.msg}."
    except ZeroDivisionError:
        return "⚠️ CALCULATOR_ERROR: division by zero."
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("CALCULATOR_LIMIT:"):
            return f"⚠️ {msg}"
        return f"⚠️ CALCULATOR_ERROR: {msg}."
    except Exception as exc:
        return f"⚠️ CALCULATOR_ERROR: {type(exc).__name__}: {exc}"
    return str(result)


def get_tools():
    return [spec_from_callable(_calculate)]
