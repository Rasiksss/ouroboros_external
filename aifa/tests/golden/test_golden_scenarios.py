"""Состав набора приёмки: сценарии A1–A8 и S1–S5 читаются клиентом записи (ADR-0005, ADR-0033)."""

from __future__ import annotations

from pathlib import Path

import pytest
from golden_record import load_scenario

HERE = Path(__file__).resolve().parent
ACCEPTANCE = [*(f"A{number}" for number in range(1, 9)), *(f"S{number}" for number in range(1, 6))]
# S0 — «открыть сохранённый чат»: хода нет, записывать нечего (golden/README.md).
TWO_TURN = {"S4": 2, "S5": 2}


@pytest.mark.parametrize("name", ACCEPTANCE)
def test_scenario_loads_with_existing_documents_and_turns(name: str) -> None:
    """ADR-0033 п. 4: набор определяется 34-status.md; файлы лежат в репозитории."""
    scenario = load_scenario(HERE / name / "scenario.json")
    assert scenario.name == name
    assert len(scenario.turns) == TWO_TURN.get(name, 1)
    assert all(turn.strip() for turn in scenario.turns)
    assert scenario.documents and all(path.is_file() for path in scenario.documents)
    assert scenario.web_search is False, "прогоны частей 12 и 15 шли без веб-поиска"


def test_s4_and_s5_extract_before_they_ask_for_the_extracted_value() -> None:
    """Part 15: S4/S5 работают на `Case`, который создал ход извлечения в том же чате."""
    for name in TWO_TURN:
        first, second = load_scenario(HERE / name / "scenario.json").turns
        assert first.startswith("Извлеки выручку") and "уже извлечённ" in second.lower()


def test_acceptance_documents_are_the_two_public_reports_by_name() -> None:
    names = {
        path.name
        for name in ACCEPTANCE
        for path in load_scenario(HERE / name / "scenario.json").documents
    }
    assert names == {"Novatek_FS_12m2021_RUS.pdf", "lukoil_2026-06-30.pdf"}
