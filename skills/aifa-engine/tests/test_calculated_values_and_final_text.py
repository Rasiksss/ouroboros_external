"""Правила после чтения Excel из dev-прогона 61b91465: компоненты расчётных показателей, итог по `facts`."""

from aifa_engine_support import SKILL_DIR

SKILLS = SKILL_DIR.parent


def text(relative: str) -> str:
    return " ".join((SKILLS / relative).read_text(encoding="utf-8").split())


def test_author_writes_components_of_a_calculated_metric_into_the_file() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "Компоненты расчётного показателя выводи в файл всегда" in source
    assert "`derived_fact.expression`" in source
    assert "компоненты не приведены" in source


def test_author_returns_facts_as_strings_taken_from_the_same_values_as_the_cells() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "`facts`" in source
    assert "массив строк" in source
    assert "из тех же значений, что и ячейки" in source


def test_author_names_an_operand_that_is_narrower_or_out_of_scale() -> None:
    source = text("aifa-atom-author/SKILL.md")
    assert "несоразмерна" in source
    assert "не прячь" in source


def test_root_accepts_a_calculated_metric_only_after_reading_its_operands() -> None:
    source = text("aifa-planning/references/plans.md")
    assert 'view="full"' in source
    assert "охват" in source
    assert "несоразмерна" in source
    assert "не подтверждён" in source


def test_root_answers_with_numbers_only_from_facts_or_the_artifact_table() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "`payload.facts`" in source
    assert "Ключевые цифры" in source
    checking = text("aifa-planning/references/plans.md")
    assert "сравнение двух чисел — тоже расчёт" in checking
    assert "сверь каждое число" in checking


def test_root_does_not_attach_a_second_copy_of_the_published_file() -> None:
    source = text("aifa-planning/SKILL.md")
    assert "второй, непроверенный экземпляр" in source
    assert "`file_read` на него" in source
