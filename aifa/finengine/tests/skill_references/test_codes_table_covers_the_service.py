"""Таблица кодов навыка `aifa-planning` покрывает коды службы (шаг 14, ревью 14-r4, З-r4-4).

Три круга ревью нашли в тексте навыка код ответа без указания или с двумя несовместимыми. Корень —
проверка по фразам, а не по покрытию: этот тест требует строку таблицы `references/codes.md` на каждый
код и каждую пару «инструмент × код», которые умеет отдавать служба.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from finengine.assistant import ouro_view

ASSISTANT = Path(ouro_view.__file__).parent
CODES = Path(__file__).resolve().parents[4] / "skills" / "aifa-planning" / "references" / "codes.md"
# Коды инструментов шагов 16–17 (`aifa_register_atom`, `aifa_report`, подзадачи): их инструментов в навыке пока нет.
RESERVED = {"лимит_попыток", "лимит_отчётов", "подзадача_не_ответила", "не_та_роль"}
# Что отдаёт каждый инструмент (по коду `goal_tools`, `loop_tools`, `read_tools`, `tool_api`, `jobs`).
ANSWERS = {
    "aifa_goal": [
        "ГОТОВО",
        "ЖДИ",
        "УТОЧНИ",
        "НУЖЕН_ЦИКЛ",
        "СБОЙ_СЛУЖБЫ",
        "нет_документа",
        "неверные_аргументы",
        "чужой_ход",
    ],
    "aifa_wait": ["ЖДИ", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "задание_не_найдено", "чужой_ход"],
    "aifa_state": ["ГОТОВО", "СБОЙ_СЛУЖБЫ", "неверные_аргументы", "нет_цикла", "чужой_ход"],
    "aifa_doc": [
        "ГОТОВО",
        "СБОЙ_СЛУЖБЫ",
        "нет_документа",
        "неверные_аргументы",
        "нет_цикла",
        "чужой_ход",
    ],
    "aifa_move": [
        "ГОТОВО",
        "ЖДИ",
        "ЛИМИТ",
        "СБОЙ_СЛУЖБЫ",
        "неверные_аргументы",
        "нет_цикла",
        "цикл_закрыт",
        "повтор_тупика",
        "нет_входов",
        "лимит_атомов",
        "не_dyn_тип",
        "нет_единицы",
        "чужой_ход",
    ],
    "aifa_finish": [
        "ПРИНЯТО",
        "ЖДИ",
        "СБОЙ_СЛУЖБЫ",
        "неверные_аргументы",
        "нет_цикла",
        "цикл_закрыт",
        "файл_не_собран",
        "чужой_ход",
    ],
}
MODULE_TOOLS = {
    "goal_tools.py": ("aifa_goal", "aifa_wait"),
    "loop_tools.py": ("aifa_move", "aifa_finish"),
    "read_tools.py": ("aifa_state", "aifa_doc"),
}


def rows() -> list[tuple[str, str]]:
    """(код, инструменты) каждой строки таблицы."""
    lines = [
        line for line in CODES.read_text(encoding="utf-8").splitlines() if line.startswith("| `")
    ]
    return [
        (cells[0], cells[1])
        for cells in (
            tuple(cell.strip() for cell in line.strip("|").split("|", 2)) for line in lines
        )
    ]


def has_row(code: str, tool: str) -> bool:
    return any(code in row_code and f"`{tool}`" in tools for row_code, tools in rows())


def test_every_code_and_refusal_of_the_service_has_a_row() -> None:
    table = " ".join(row_code for row_code, _ in rows())
    values = [str(code) for code in ouro_view.Code if code is not ouro_view.Code.REFUSE]
    values += [str(refusal) for refusal in ouro_view.Refusal if str(refusal) not in RESERVED]

    assert [value for value in values if value not in table] == []


@pytest.mark.parametrize("tool", sorted(ANSWERS))
def test_every_answer_a_tool_gives_has_a_row_that_names_the_tool(tool: str) -> None:
    assert [code for code in ANSWERS[tool] if not has_row(code, tool)] == []


@pytest.mark.parametrize("module", sorted(MODULE_TOOLS))
def test_a_refusal_raised_in_a_tool_module_is_listed_for_one_of_its_tools(module: str) -> None:
    """Новый `Refusal.X` в модуле инструмента без строки таблицы краснит набор, пока строку не добавят."""
    source = (ASSISTANT / module).read_text(encoding="utf-8")
    names = set(re.findall(r"Refusal\.([A-Z_]+)", source))
    values = {refusal.name: str(refusal) for refusal in ouro_view.Refusal}
    missing = [
        values[name]
        for name in sorted(names)
        if values[name] not in RESERVED
        and not any(has_row(values[name], tool) for tool in MODULE_TOOLS[module])
    ]

    assert missing == []
