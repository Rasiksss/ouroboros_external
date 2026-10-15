"""Сборка `RunPassport` (Срез 1, B1) — «Критерий готовности связки классификации»
из docs/build-plan/11-srez-01-run-passport.md:

2. После фиксации `RunPassport` его сериализованное значение неизменно
   передаётся в извлечение, расчёт и анализ — здесь: три вызова
   `build_run_passport` с одинаковыми входами (как их сделали бы
   `fe_classify_report`, `aifa-extract`, `aifa-analyze`) дают одинаковый
   `classification`/`entity`.
3. После фиксации паспорта в рабочем пути отсутствуют вызовы `detect_profile()`,
   когда `--cell` уже решена.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from finengine.skills.atoms import frame as frame_module
from finengine.skills.atoms.frame import classify_frame
from finengine.skills.atoms.run_passport import _CELL_TO_AXES, build_run_passport, classification_from_cell
from finengine.skills.extractors._classifier import PROFILE_IDS, _compose_profile_id

_TEXT = "ПАО «Ромашка» — консолидированная финансовая отчетность по МСФО за 2025 год."


def test_cell_axes_table_matches_the_classifier():
    assert set(_CELL_TO_AXES) == set(PROFILE_IDS)
    for cell, (standard, company_type, language) in _CELL_TO_AXES.items():
        assert _compose_profile_id(standard, company_type, language) == cell


def test_classification_from_cell_rejects_unknown_cell():
    with pytest.raises(ValueError):
        classification_from_cell("not_a_real_cell")


def test_classification_from_cell_applies_company_type_override():
    result = classification_from_cell("ifrs_nf_ru", company_type_override="bank")
    assert result.company_type == "bank"
    assert result.profile_id == "ifrs_nf_ru"  # ячейка НЕ пересобирается (как в extract.py сегодня)


def test_classification_from_cell_never_calls_detect_profile():
    with patch.object(frame_module, "detect_profile") as mocked:
        classification_from_cell("ifrs_nf_ru")
    mocked.assert_not_called()


def test_build_run_passport_with_a_cell_does_not_call_detect_profile():
    """DoD: «в рабочем пути отсутствуют вызовы detect_profile()», когда `--cell` дана."""
    with patch.object(frame_module, "detect_profile") as mocked:
        passport = build_run_passport(
            _TEXT, goal_ir_id="doc-1", registry_version="reg-1", cell="ifrs_nf_ru",
        )
    mocked.assert_not_called()
    assert passport.classification.profile_id == "ifrs_nf_ru"


def test_build_run_passport_without_a_cell_classifies_once():
    passport = build_run_passport(_TEXT, goal_ir_id="doc-1", registry_version="reg-1")
    assert passport.classification.profile_id == classify_frame(_TEXT).profile_id


def test_the_same_inputs_produce_the_same_serialized_passport_across_boundaries():
    """Три независимых вызова (как из fe_classify_report / extract.py / analyze.py)
    с одними и теми же входами обязаны дать один и тот же паспорт по значению —
    гарантия держится на детерминизме сборки, не на разделяемой памяти между
    отдельными процессами станции."""
    kwargs = dict(
        goal_ir_id="doc-42",
        registry_version="reg-2026.09:deadbeef",
        filename="romashka_2025.pdf",
        company_hint="Ромашка",
        cell="ifrs_nf_ru",
    )
    from_tool = build_run_passport(_TEXT, **kwargs)
    from_extract = build_run_passport(_TEXT, **kwargs)
    from_analyze = build_run_passport(_TEXT, **kwargs)

    assert from_tool.classification == from_extract.classification == from_analyze.classification
    assert from_tool.entity == from_extract.entity == from_analyze.entity
    assert from_tool.registry_version == from_extract.registry_version == from_analyze.registry_version
    # `run_id`/`created_at` намеренно РАЗНЫЕ у каждого вызова (каждый вызов — новый
    # объект контракта); паспорт «тот же» по СОДЕРЖАНИЮ, не по идентичности объекта.
    assert from_tool.run_id != from_extract.run_id


def test_passport_is_frozen_and_carries_all_required_versions():
    passport = build_run_passport(
        _TEXT, goal_ir_id="doc-1", registry_version="reg-1",
        methodology_version="m-1", model_version="qwen/qwen3.6-plus",
    )
    assert passport.methodology_version == "m-1"
    assert passport.model_version == "qwen/qwen3.6-plus"
    assert passport.prompt_version  # непустая заглушка, а не выдуманный номер
    assert passport.sandbox_image_version
    with pytest.raises(Exception, match="frozen"):
        passport.registry_version = "tampered"
