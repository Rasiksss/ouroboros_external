"""Навык `aifa-atom-author` отвечает на каждый код, который служба отдаёт подзадаче, и называет её пределы (P118, ревью 17 З6).

Подзадача читает только этот навык (`aifa-planning` ей читать запрещено), поэтому код ответа без указания или предел,
названный неверно, — ошибка текста, а не мелочь. Тест берёт коды и пределы из кода службы, а не из памяти ревьюера.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from finengine.assistant import atom_tools, ouro_view
from finengine.skills.spec import atom_intake, ephemeral

SKILL = Path(__file__).resolve().parents[4] / "skills" / "aifa-atom-author" / "SKILL.md"
ASSISTANT = Path(atom_tools.__file__).parent
INTAKE = Path(atom_intake.__file__)
# Остальное, что отвечает инструмент подзадачи: коды `ouro_view.Code`, привязка и очередь цикла.
GENERAL = {"ЛИМИТ", "СБОЙ_СЛУЖБЫ", "ЖДИ", "нет_цикла", "чужой_ход"}
# Отказы, которые подзадача получает не от своих инструментов сдачи и отчёта.
ELSEWHERE = {"подзадача_не_ответила", "задание_не_найдено", "повтор_тупика", "файл_не_собран", "нет_документа"}


def skill_text() -> str:
    return " ".join(SKILL.read_text(encoding="utf-8").split())


def refusals_named_in(path: Path) -> set[str]:
    """`Refusal.X` в исходнике, переведённые в значение кода отказа."""
    values = {refusal.name: str(refusal) for refusal in ouro_view.Refusal}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        values[node.attr]
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Refusal"
        and node.attr in values
    }  # fmt: skip


def codes_of_the_intake() -> set[str]:
    """Коды из `Refused("…", …)` и из таблицы `_REFUSAL_MARKS` приёмки."""
    tree = ast.parse(INTAKE.read_text(encoding="utf-8"))
    found = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Refused"
        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    }  # fmt: skip
    return found | {code for _, code in atom_intake._REFUSAL_MARKS}


def test_the_skill_answers_every_code_the_service_gives_to_a_subtask() -> None:
    codes = (refusals_named_in(ASSISTANT / "atom_tools.py") | codes_of_the_intake() | GENERAL) - ELSEWHERE
    body = skill_text()

    assert codes, "коды подзадачи не найдены: тест потерял исходник"
    assert [code for code in sorted(codes) if code not in body] == []


def test_the_skill_names_the_limits_of_the_report_as_the_service_has_them() -> None:
    body = skill_text()

    assert f"`label` — не длиннее {atom_tools.LABEL_MAX} знаков" in body
    assert f"`note` — не длиннее {atom_tools.NOTE_MAX}" in body


def test_the_skill_names_the_name_rule_as_the_service_has_it() -> None:
    low, high = re.search(r"\{(\d+),(\d+)\}", ephemeral._NAME.pattern).groups()  # type: ignore[union-attr]

    assert f"от {low} до {high} знаков" in skill_text()
