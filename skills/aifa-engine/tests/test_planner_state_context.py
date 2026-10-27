import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def text_of(skill: str) -> str:
    return re.sub(r"\s+", " ", (ROOT / skill / "SKILL.md").read_text(encoding="utf-8"))


def test_the_planner_reads_all_registry_pages_before_deciding_a_metric_is_missing():
    text = text_of("aifa-plan-author")
    assert "registry_offset" in text and "next_offset" in text
    assert "все страницы реестра" in text


def test_the_example_combines_statistics_and_excel_after_the_page_has_been_loaded():
    text = text_of("aifa-plan-author")
    example = text.split("- Пример.", 1)[1].split("- Шаг,", 1)[0]
    assert "атом-загрузка" in example
    assert "один шаг-автор" in example
    assert "статистики" in example and "Excel" in example
    assert "Ход 2 — шаг-автор считает" not in example
