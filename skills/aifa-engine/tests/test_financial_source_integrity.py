"""Ограничения навыка после проверки Excel из dev-прогона c9a290dc."""

from aifa_engine_support import SKILL_DIR

PLANNING = SKILL_DIR.parent / "aifa-planning"


def text(relative: str) -> str:
    return " ".join((PLANNING / relative).read_text(encoding="utf-8").split())


def test_missing_operand_is_not_replaced_by_a_nearby_metric() -> None:
    source = text("references/plans.md")
    assert "близким по названию показателем" in source
    assert "эквивалентность подтверждена источником" in source


def test_external_series_distinguishes_forecast_and_observed_periods() -> None:
    source = text("references/external-data.md")
    assert "Прогноз не выдавай за фактическое значение" in source
    assert "полный год и часть года" in source
    assert "дату обращения" in source


def test_file_acceptance_checks_provenance_and_conclusions() -> None:
    source = text("references/plans.md")
    assert "ссылку на страницу документа" in source
    assert "источники всех операндов" in source
    assert "направление изменений в выводах" in source
