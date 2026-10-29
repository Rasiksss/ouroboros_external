"""Ограничения навыка после проверки Excel из dev-прогона c9a290dc."""

from aifa_engine_support import SKILL_DIR

PLANNING = SKILL_DIR.parent / "aifa-planning"


def text(relative: str) -> str:
    return " ".join((PLANNING / relative).read_text(encoding="utf-8").split())


def test_external_series_distinguishes_forecast_and_observed_periods() -> None:
    source = text("references/external-data.md")
    assert "Прогноз не выдавай за фактическое значение" in source
    assert "полный год и часть года" in source
    assert "дату обращения" in source
