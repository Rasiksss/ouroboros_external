"""C4.7, PRINCIPLES А.5: служба не знает навыка подзадачи (ADR-0041): ни ветки, ни строки с его именем.

Подзадачу служба видит как задачу ouroboros хода. Проверка смотрит код (не комментарии и докстринги) модулей, через
которые проходят операции подзадач, на имена навыков и на разбор задания «Навык: <id>.».
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "finengine"
MODULES = (
    "assistant/subtask_waits.py", "assistant/atom_tools.py", "assistant/goal_tools.py", "assistant/tool_api.py",
    "assistant/loop_tools.py", "assistant/binding.py", "assistant/turn_gateway.py", "skills/spec/atom_intake.py",
)
SKILL_MARKS = ("aifa-atom-author", "aifa-planning", "contract-fixture", "distribution:", "Навык:")


def _code_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


@pytest.mark.parametrize("module", MODULES)
def test_the_code_of_the_subtask_operations_names_no_skill(module: str) -> None:
    """C4.7: в строках кода нет имени навыка и разбора задания по нему."""
    named = [text for text in _code_strings(SRC / module) if any(mark in text for mark in SKILL_MARKS)]

    assert named == [], f"{module}: служба знает навык подзадачи (ADR-0041): {named}"


def test_the_scan_sees_the_modules() -> None:
    """Страховка: пустое множество модулей — зелёная проверка."""
    assert all((SRC / module).is_file() for module in MODULES)
