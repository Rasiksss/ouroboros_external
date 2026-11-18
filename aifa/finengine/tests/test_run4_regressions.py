import importlib

import pytest

from test_paths_layout import finengine_importable  # noqa: F401

pytestmark = pytest.mark.usefixtures("finengine_importable")


def test_formula_calculates_without_removed_registry_directory():
    versioning = importlib.import_module("finengine.skills.versioning")
    registry = versioning.registry_fingerprint("ifrs_bank_ru")
    assert registry["sha256"] and not registry.get("problem")
    from finengine.skills.ratios import compute_ratios
    result = compute_ratios(
        [{"enum": "A", "value_current": 6, "value_prev": 4, "unit": "млн руб."},
         {"enum": "B", "value_current": 3, "value_prev": 2, "unit": "млн руб."}],
        "ifrs_bank_ru", only=["TEST_RATIO"], period_current="FY 2024", period_prev="FY 2023",
        extra=[{"enum": "TEST_RATIO", "formula": "A / B", "dependencies": ["A", "B"]}],
    )
    assert result and result[0]["value_current"] == pytest.approx(2)


def test_interbank_borrowings_hint_matches_liability():
    from finengine.skills.metric_schema import compiled_registry_for_profile
    registry = compiled_registry_for_profile("ifrs_bank_ru").to_effective_data()
    row = next(row for row in registry["extractable"] if row["enum"] == "INTERBANK_LOANS")
    assert "обязательств" in row["hint"].lower()
    assert "выданные" not in row["description"].lower()
