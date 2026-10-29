"""Навыки о каталоге атомов (ADR-0064): порядок «каталог → существующее → пробел → поручить», без жёстких предписаний."""

from __future__ import annotations

import pathlib
import re

from aifa_engine_support import SKILL_DIR, load_plugin

SKILLS = SKILL_DIR.parent
PLANNING = SKILLS / "aifa-planning"
ALL_TEXTS = [
    PLANNING / "SKILL.md",
    *sorted((PLANNING / "references").glob("*.md")),
    SKILLS / "aifa-plan-author" / "SKILL.md",
    SKILLS / "aifa-atom-author" / "SKILL.md",
]
# Предписания, которые решали за агента до чтения контракта: «такой файл — только кодовому автору».
HARD_RULES = (
    "сразу пункт 5",
    "`out.file` не подходит — сразу",
    "такой файл не собрать",
    "стандартный `out.file` не собирает",
    "ни сценария, ни атома каталога для него нет",
)


def read(path: pathlib.Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def guide() -> str:
    return read(PLANNING / "SKILL.md")


def reference(name: str) -> str:
    return read(PLANNING / "references" / name)


def test_the_plugin_gives_the_tool_to_the_skills_that_name_it() -> None:
    tools = {tool.name for tool in load_plugin().TOOLS}

    assert "aifa_atoms" in tools
    for path in ALL_TEXTS:
        assert set(re.findall(r"`(aifa_\w+)", read(path))) <= tools, path.name


def test_the_root_orders_the_plan_checks_it_by_the_cards_then_delegates_the_gap() -> None:
    """ADR-0064 п. 4–5 в редакции ADR-0084: каталог изучает планировщик, корень проверяет предложение по карточкам."""
    text = guide()
    order = "закажи план → проверь предложение по карточкам → при пробеле поручи создание атома"

    assert order in text and '`aifa_atoms(action="list")`' in text
    assert text.index(order) < text.index("### Делегирование")
    assert "пробел" in text.split("### Делегирование", 1)[1].split("### Завершение", 1)[0]


def test_the_root_reads_the_code_of_an_atom_when_the_card_is_not_enough() -> None:
    assert '`aifa_atoms(action="get", name=…)`' in guide()


def test_a_gap_is_found_by_the_contracts_and_not_by_the_kind_of_the_result() -> None:
    """ADR-0064 п. 5: «Excel с двумя листами» не требует кодового агента сам по себе — это решает карточка атома."""
    text = " ".join(read(path) for path in ALL_TEXTS)

    assert [rule for rule in HARD_RULES if rule in text] == []
    assert "по карточкам" in guide() and "а не по виду результата" in guide()


def test_the_task_for_the_code_author_carries_the_five_parts() -> None:
    """ADR-0064 п. 4: назначение, входы, ожидаемый результат, ограничения и критерии приёмки."""
    section = guide().split("### Делегирование", 1)[1].split("### Завершение", 1)[0]

    for part in ("назначение", "входы", "ожидаемый результат", "ограничения", "критерии приёмки"):
        assert part in section, part


def test_an_atom_that_fits_but_has_no_inputs_yet_is_not_a_reason_for_a_new_one() -> None:
    text = " ".join([guide(), reference("new-atom.md")])

    assert "входы ещё не получены" in text and "не причина создавать новый атом" in text


def test_the_new_atom_order_starts_from_the_catalog_tool() -> None:
    order = reference("new-atom.md").split("Порядок строгий:", 1)[1].split("Атом с кодом в `new_atoms`", 1)[0]

    assert "aifa_atoms" in order and order.index("aifa_atoms") < order.index("только иначе")


def test_a_registered_atom_is_not_run_again() -> None:
    """ADR-0064 п. 6 (ADR-0060 п. 4): сдача уже исполнила атом — навыки не просят второго запуска."""
    assert "шагом его запускать не нужно" in guide()


def test_the_planner_subtask_reads_the_catalog_and_names_gaps_for_the_root() -> None:
    """ADR-0064 п. 4: подзадача планирования предлагает существующее и называет, чего не хватает; решает корень."""
    text = read(SKILLS / "aifa-plan-author" / "SKILL.md")

    assert '`aifa_atoms(action="list")`' in text and "не хватает" in text
    assert "решение принимает корень" in text or "решает корень" in text
    forbidden = text.split("Не вызывай", 1)[1]
    assert "aifa_atoms" not in forbidden


def test_the_code_author_studies_the_existing_implementations() -> None:
    text = read(SKILLS / "aifa-atom-author" / "SKILL.md")

    assert '`aifa_atoms(action="get"' in text and "существующие реализации" in text
    assert "прежнего кода у подзадачи нет" not in text.lower()


def test_the_engine_skill_lists_the_tool_with_the_others() -> None:
    text = read(SKILL_DIR / "SKILL.md")

    assert "aifa_atoms" in text.split("version:", 1)[0]
