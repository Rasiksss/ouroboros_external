"""Термины плана и интернет (владелец, 27.10.2026).

План — набор ходов, ход — набор шагов, шаг — действие с конкретным результатом. Прогон 65ce9c83: слово «шаг» означало и
запись `task_plan`, и вызов атома; планировщик не слил независимые шаги, корень не вернул план, ходы шли подряд.
Ненайденный показатель отчётности агент сам интернетом не подменяет: сначала спрашивает пользователя."""

from __future__ import annotations

import pathlib

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent
PLANNING = SKILLS / "aifa-planning"
TERMS = "План — набор ходов. Ход — набор шагов. Шаг — действие с конкретным результатом"


def read(path: pathlib.Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def reference(name: str) -> str:
    return read(PLANNING / "references" / name)


def test_every_aifa_instruction_fixes_the_same_three_terms() -> None:
    for path in (
        PLANNING / "SKILL.md",
        SKILLS / "aifa-plan-author" / "SKILL.md",
        SKILLS / "aifa-atom-author" / "SKILL.md",
        SKILLS / "aifa-engine" / "SKILL.md",
    ):
        assert TERMS in read(path), path.parent.name


def test_the_planner_gets_the_plan_structure_as_an_important_rule_with_an_example() -> None:
    text = read(SKILLS / "aifa-plan-author" / "SKILL.md")

    assert "**ВАЖНО. Устройство плана.**" in text
    assert "План — набор связанных, последовательно зависящих друг от друга ходов." in text
    assert "Ход 2 использует результат хода 1, ход 3 — результат хода 2; параллельно эти ходы выполнить нельзя." in text
    assert "Ход — набор исключительно независимых шагов: ни один шаг не берёт результата другого, все они выполняются параллельно." in text
    assert "Пример." in text and "Ход 1 —" in text and "Ход 2 —" in text and "Ход 3 —" in text
    assert "В схеме `task_plan` ход называется `step`" in text


def test_the_root_checks_the_layout_of_the_plan_and_not_only_that_it_covers_the_goals() -> None:
    guide = read(PLANNING / "SKILL.md")
    step = reference("plans.md").split("4. Проверь план", 1)[1].split("## Исполнить и проверить", 1)[0]

    assert "проверяй не только покрытие целей пользователя, но и оформление" in guide
    assert "Проверь план по двум критериям: покрытие и оформление" in reference("plans.md")
    assert "план — цепочка ходов, ход — набор независимых шагов" in step
    assert "каждый ход после первого использует результат предыдущего" in step
    assert "в одном ходе нет шага, который берёт результат другого шага этого хода" in step
    assert "независимые шаги не разнесены по разным ходам" in step
    assert "даже если цели пользователя покрыты" in step


def test_a_dependent_part_goes_to_the_next_move_and_the_example_shows_two_moves() -> None:
    moves = reference("moves.md")
    scenarios = reference("scenarios.md")

    assert "Ход — набор независимых шагов" in moves
    assert "Часть, которой нужен результат другой части, — в следующий ход" in moves
    assert "`depends_on` называет опору, полученную ранее" in moves
    assert "Ход 1" in scenarios and "Ход 2" in scenarios


def test_a_missing_report_metric_is_not_replaced_by_the_internet_on_the_agents_own() -> None:
    artifacts = reference("artifacts.md")
    guide = read(PLANNING / "SKILL.md")
    author = read(SKILLS / "aifa-plan-author" / "SKILL.md")

    assert "показатель отчётности в интернете по своей инициативе не ищи и интернетом не подменяй" in artifacts
    assert "Показатель X в отчёте не найден; могу попробовать найти его в интернете — искать?" in artifacts
    assert "Пользователь разрешил искать" in artifacts and "интернет не разрешён или значений не дал — итог `partial`" in artifacts
    assert "сам интернетом не подменяй" in guide and "спроси пользователя, искать ли в интернете" in guide
    assert "внешний поиск не планируй, пока" in author and "искать ли в интернете" in author
    assert "Показатель отчётности компании, не найденный в отчёте" in reference("external-data.md")
