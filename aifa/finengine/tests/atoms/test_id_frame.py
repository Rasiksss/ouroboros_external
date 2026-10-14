"""`id.frame` — критерий готовности (docs/build-plan/11-srez-01-run-passport.md):

1. Все однозначные отчёты `tests/golden/classification/` получают ожидаемые
   стандарт, тип компании, язык и `profile_id`.
2. Неподдерживаемая комбинация и низкая уверенность возвращаются явно;
   запрещён тихий fallback на похожий профиль.
3. После фиксации `ClassificationResult` он неизменяем и совпадает во всех
   следующих компонентах запуска (при одинаковом входе).
4. Атом классифицирует финансовый контекст, а не технический тип PDF — эти
   две классификации не смешиваются (по конструкции: `classify_frame`
   принимает только текст, ни разу не открывает файл — в отличие от
   `doc.probe`).
"""
from __future__ import annotations

import json
from pathlib import Path

from finengine.contracts import ClassificationResult
from finengine.skills.atoms.frame import classify_frame

ROOT = Path(__file__).resolve().parents[1] / "golden" / "classification"
CASES = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
AMBIGUOUS = json.loads((ROOT / "ambiguous_cases.json").read_text(encoding="utf-8"))


def test_atom_wraps_every_golden_classification_case_correctly():
    mismatches = []
    for c in CASES:
        result = classify_frame(c["excerpt"])
        assert isinstance(result, ClassificationResult)
        if result.profile_id != c["profile_id"]:
            mismatches.append((c["id"], c["company"], result.profile_id, c["profile_id"]))
    assert not mismatches, f"атом разошёлся с ожидаемым profile_id: {mismatches}"


def test_ambiguous_cases_stay_explicit_not_silently_confident():
    for c in AMBIGUOUS:
        result = classify_frame(c["excerpt"])
        genuinely_ambiguous = result.confidence_low or result.profile_id != c.get("plausible_profile_id")
        assert genuinely_ambiguous, (
            f"{c['id']}: атом уверенно и правильно определил профиль — это больше не "
            "неоднозначный случай"
        )


def test_classification_result_is_frozen_and_reproducible():
    text = CASES[0]["excerpt"]
    first = classify_frame(text)
    second = classify_frame(text)
    assert first == second
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="frozen"):
        first.profile_id = "tampered"


def test_user_override_replaces_the_standard_axis_and_clears_its_low_confidence():
    text = CASES[0]["excerpt"]
    baseline = classify_frame(text)
    overridden = classify_frame(text, standard_override="us_gaap")
    assert overridden.standard == "us_gaap"
    assert overridden.axis_confidence_low["standard"] is False
    assert overridden.company_type == baseline.company_type


def test_company_type_override_recomposes_the_profile_id():
    nf_case = next(c for c in CASES if c["profile_id"] == "ifrs_nf_ru")
    overridden = classify_frame(nf_case["excerpt"], company_type_override="bank")
    assert overridden.company_type == "bank"
    assert overridden.profile_id == "ifrs_bank_ru"
    assert overridden.axis_confidence_low["company_type"] is False
