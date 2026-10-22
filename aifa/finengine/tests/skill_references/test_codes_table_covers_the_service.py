"""Таблица кодов навыка `aifa-planning` покрывает коды службы (шаг 14, ревью 14-r4 З-r4-4, 14-r5 Д1, Д2).

Пять кругов ревью находили в тексте навыка код ответа без указания или с двумя несовместимыми. Корень —
проверка по фразам: этот тест требует строку таблицы `references/codes.md` на каждый код и каждую пару
«инструмент × код», которые умеет отдавать служба, условие строки — из закрытого словаря, а на одну клетку
«код × инструмент × условие» — ровно одна строка (две строки одной клетки — это два правила).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from finengine.assistant import ouro_view

ASSISTANT = Path(ouro_view.__file__).parent
CODES = Path(__file__).resolve().parents[4] / "skills" / "aifa-planning" / "references" / "codes.md"
CONDITIONS = {
    "всегда", "вне цикла", "в цикле", "до ЛИМИТ", "документа нет", "текст не готов", "свои аргументы",
    "итог ждавшего вызова",
}  # fmt: skip
# Коды подзадачи (`aifa_register_atom`, `aifa_report`): корень их не получает, их разбирает навык подзадачи
# (`skills/aifa-atom-author`); в таблице корня — только `нет_входов`, который виден в итоге подзадачи.
RESERVED = {"лимит_попыток", "лимит_отчётов", "не_та_роль"}
# Что отдаёт каждый инструмент помимо отказов, видных в его исходнике (`Refusal.X` внутри класса инструмента):
# коды `ouro_view.Code`, отказы `check`/`move` цикла, отказы `tool_api` и привязки.
ANSWERS = {
    "aifa_open": ["ГОТОВО", "ЖДИ", "СБОЙ_СЛУЖБЫ", "нет_документа", "чужой_ход"],
    "aifa_wait": [
        "ГОТОВО", "ЖДИ", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "задание_не_найдено", "подзадача_не_ответила", "чужой_ход",
    ],
    "aifa_state": ["ГОТОВО", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "нет_цикла", "чужой_ход"],
    "aifa_doc": ["ГОТОВО", "СБОЙ_СЛУЖБЫ", "нет_документа", "неверные_аргументы", "нет_цикла", "чужой_ход"],
    "aifa_atoms": ["ГОТОВО", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "нет_цикла", "чужой_ход"],
    "aifa_move": [
        "ГОТОВО", "ЖДИ", "ЛИМИТ", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "нет_цикла", "цикл_закрыт", "повтор_тупика",
        "лимит_атомов", "не_dyn_тип", "нет_единицы", "чужой_ход",
    ],
    "aifa_finish": [
        "ПРИНЯТО", "ЖДИ", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "нет_цикла", "цикл_закрыт", "файл_не_собран", "чужой_ход",
    ],
}  # fmt: skip
MODULES = ("goal_tools.py", "loop_tools.py", "read_tools.py", "atoms_tool.py")


class Row:
    def __init__(self, code: str, tools: str, condition: str, action: str) -> None:
        self.code, self.tools, self.condition, self.action = code, tools, condition, action

    def names(self, tool: str) -> bool:
        return f"`{tool}`" in self.tools


def rows() -> list[Row]:
    lines = [
        line for line in CODES.read_text(encoding="utf-8").splitlines() if line.startswith("| `")
    ]
    parsed = [[cell.strip() for cell in line.strip().strip("|").split("|", 3)] for line in lines]
    assert all(len(cells) == 4 for cells in parsed), "в строке таблицы должно быть четыре столбца"
    return [Row(*cells) for cells in parsed]


def has_row(code: str, tool: str) -> bool:
    return any(code in row.code and row.names(tool) for row in rows())


def tool_name(node: ast.ClassDef, constants: dict[str, str]) -> str | None:
    """Имя инструмента из `name = "aifa_x"` или `name = КОНСТАНТА` в теле класса."""
    for stmt in node.body:
        if not (
            isinstance(stmt, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "name" for t in stmt.targets)
        ):
            continue
        value = stmt.value
        name = (
            value.value
            if isinstance(value, ast.Constant)
            else constants.get(getattr(value, "id", ""), "")
        )
        if str(name).startswith("aifa_"):
            return str(name)
    return None


def direct_refusals() -> dict[str, set[str]]:
    """Отказы, которые инструмент отдаёт сам: `Refusal.X` в теле его класса и вызов `no_loop()` (= `нет_цикла`)."""
    values = {refusal.name: str(refusal) for refusal in ouro_view.Refusal}
    found: dict[str, set[str]] = {}
    for module in MODULES:
        tree = ast.parse((ASSISTANT / module).read_text(encoding="utf-8"))
        constants = {
            target.id: stmt.value.value
            for stmt in tree.body
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
            for target in stmt.targets
            if isinstance(target, ast.Name) and isinstance(stmt.value.value, str)
        }
        for node in ast.walk(tree):
            tool = tool_name(node, constants) if isinstance(node, ast.ClassDef) else None
            if tool is None or not isinstance(node, ast.ClassDef):
                continue
            refusals = {
                values[sub.attr]
                for sub in ast.walk(node)
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "Refusal"
                and sub.attr in values
            }  # fmt: skip
            if any(isinstance(sub, ast.Name) and sub.id == "no_loop" for sub in ast.walk(node)):
                refusals.add(values["NO_LOOP"])
            found[tool] = refusals
    return found


def test_every_code_and_refusal_of_the_service_has_a_row() -> None:
    table = " ".join(row.code for row in rows())
    values = [str(code) for code in ouro_view.Code if code is not ouro_view.Code.REFUSE]
    values += [str(refusal) for refusal in ouro_view.Refusal if str(refusal) not in RESERVED]

    assert [value for value in values if value not in table] == []


@pytest.mark.parametrize("tool", sorted(ANSWERS))
def test_every_answer_a_tool_gives_has_a_row_that_names_the_tool(tool: str) -> None:
    assert [code for code in ANSWERS[tool] if not has_row(code, tool)] == []


def test_the_refusals_found_in_the_tool_classes_are_a_part_of_the_declared_answers() -> None:
    """Перечень `ANSWERS` не отстаёт от исходников: отказ, добавленный в класс инструмента, надо внести и туда."""
    found = direct_refusals()

    assert set(found) == set(ANSWERS), (
        "в исходниках нашёлся инструмент без записи в ANSWERS или наоборот"
    )
    assert {
        tool: sorted(set(codes) - set(ANSWERS[tool]) - RESERVED) for tool, codes in found.items()
    } == {tool: [] for tool in ANSWERS}


def test_the_condition_column_uses_the_closed_vocabulary() -> None:
    assert sorted({row.condition for row in rows()} - CONDITIONS) == []


def test_one_cell_of_code_tool_and_condition_has_exactly_one_row() -> None:
    """Ревью 14-r5, Д1: две строки одной клетки — два правила, из которых модель выберет любое."""
    seen: dict[tuple[str, str, str], str] = {}
    clashes = []
    for row in rows():
        for tool in (cell.strip("` ") for cell in row.tools.split(",")):
            key = (row.code, tool, row.condition)
            if key in seen:
                clashes.append(key)
            seen[key] = row.action

    assert clashes == []


def test_a_tool_with_several_rows_for_one_code_tells_them_apart_by_condition() -> None:
    """Строки одного кода и инструмента с условием «всегда» не соседствуют с другими: «всегда» перекрывает всё."""
    by_cell: dict[tuple[str, str], set[str]] = {}
    for row in rows():
        for tool in (cell.strip("` ") for cell in row.tools.split(",")):
            by_cell.setdefault((row.code, tool), set()).add(row.condition)

    assert [
        cell
        for cell, conditions in by_cell.items()
        if "всегда" in conditions and len(conditions) > 1
    ] == []
