"""`explain.external` — критерий готовности атома (16b):

1. Каждый внешний факт имеет источник, URL, дату и связь с сущностью/
   юрисдикцией.
2. Юрисдикция определяется по `RunPassport` (`jurisdiction_of`): регистрационные
   номера эмитента, национальный стандарт, ячейка профиля МСФО. Паспорт в тестах
   строится из настоящей ячейки (`classification_from_cell`): ось `standard` в
   бою — `ifrs`, а не id ячейки `ifrs_nf_ru` (часть 19 доводки: прежний тест
   подавал id ячейки и не видел, что для МСФО атом всегда пуст).
3. Юрисдикция не определена — `None`: факты в этом случае не попадают в
   доверенное объяснение (не подставляется угадка).
4. Отсутствие внешнего доступа/находок — пустой список, не ошибка.
"""
from __future__ import annotations

from finengine.contracts import (
    ClassificationResult,
    EntityIdentity,
    ExternalFactRef,
    RunPassport,
)
from finengine.skills.analysis.external import ExternalFact, jurisdiction_of
from finengine.skills.atoms.explain_external import explain_external_executor
from finengine.skills.atoms.run_passport import _CELL_TO_AXES, classification_from_cell
from finengine.skills.spec.atom_registry import NodeArgs


UNKNOWN = ClassificationResult(
    profile_id="test-cell", standard="some_unmapped_standard", company_type="non_financial", language="ru",
    reason="test fixture",
)


def _passport(cell: str = "ifrs_nf_ru", *, classification=None, external_ids=None) -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=classification or classification_from_cell(cell),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Ромашка", raw_name="ПАО Ромашка",
                              external_ids=external_ids or {}),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


class _Ctx:
    def __init__(self, run_passport):
        self.run_passport = run_passport

    def dependencies(self):
        return ()


def test_jurisdiction_for_every_cell_the_classifier_can_put_into_the_passport():
    expected = {
        "ifrs_nf_ru": "RU", "ifrs_bank_ru": "RU", "rsbu_nf_ru": "RU", "rsbu_bank_ru": "RU",
        "us_gaap_nf_en": "US", "us_gaap_bank_en": "US", "ind_as_nf_en": "IN", "ind_gaap_bank_en": "IN",
        # МСФО на английском: стандарт международный, ячейка страну не называет — честное «не определена»
        "ifrs_nf_en": None, "ifrs_bank_en": None,
    }
    assert set(expected) == set(_CELL_TO_AXES)
    assert {cell: jurisdiction_of(_passport(cell)) for cell in expected} == expected
    assert jurisdiction_of(_passport(classification=UNKNOWN)) is None


def test_registration_numbers_of_the_issuer_outrank_the_standard_and_the_profile():
    assert jurisdiction_of(_passport("ifrs_nf_en", external_ids={"inn": "7708004767"})) == "RU"
    assert jurisdiction_of(_passport("ifrs_nf_ru", external_ids={"cik": "1234567"})) == "US"
    # номера двух стран сразу (российский эмитент с листингом в США) ничего не решают — дальше по правилу
    assert jurisdiction_of(_passport("ifrs_nf_ru", external_ids={"inn": "7708004767", "cik": "1"})) == "RU"
    assert jurisdiction_of(_passport("ifrs_nf_en", external_ids={"lei": "X" * 20})) is None


def test_an_ifrs_report_in_russian_gets_its_external_facts():
    """Часть 19: `standard="ifrs"`, `language="ru"` — как в паспорте настоящего МСФО-отчёта."""
    async def fake_collector(**kwargs):
        assert kwargs["jurisdiction"] == "RU"
        return [ExternalFact(text="Ромашка открыла завод.", source="interfax.ru",
                             url="https://interfax.ru/1", date="2025-03-01")]

    passport = _passport("ifrs_nf_ru")
    assert (passport.classification.standard, passport.classification.language) == ("ifrs", "ru")

    result = explain_external_executor(NodeArgs(period_ref="2025-Q1"), _Ctx(passport), collector=fake_collector)

    assert [ref.jurisdiction for ref in result] == ["RU"]


def test_facts_get_entity_and_jurisdiction_attached():
    async def fake_collector(**kwargs):
        assert kwargs["jurisdiction"] == "RU"
        return [ExternalFact(text="Регулятор повысил ставку.", source="cbr.ru",
                              url="https://cbr.ru/1", date="2025-03-01")]

    ctx = _Ctx(run_passport=_passport("ifrs_nf_ru"))
    result = explain_external_executor(NodeArgs(period_ref="2025-Q1"), ctx, collector=fake_collector)
    assert len(result) == 1
    ref = result[0]
    assert isinstance(ref, ExternalFactRef)
    assert ref.entity_id == "ent-1"
    assert ref.jurisdiction == "RU"
    assert ref.source == "cbr.ru"


def test_unknown_jurisdiction_blocks_facts_from_trusted_explanation():
    async def fake_collector(**kwargs):
        assert kwargs["jurisdiction"] is None
        return [ExternalFact(text="Новость без определённой юрисдикции.", source="example.com",
                              url="https://example.com/1", date="2025-03-01")]

    ctx = _Ctx(run_passport=_passport(classification=UNKNOWN))
    result = explain_external_executor(NodeArgs(period_ref="2025-Q1"), ctx, collector=fake_collector)
    assert result == []


def test_no_hits_gives_empty_list_not_error():
    async def fake_collector(**kwargs):
        return []

    ctx = _Ctx(run_passport=_passport())
    result = explain_external_executor(NodeArgs(period_ref="2025-Q1"), ctx, collector=fake_collector)
    assert result == []


def test_raises_without_run_passport():
    import pytest

    ctx = _Ctx(run_passport=None)
    with pytest.raises(ValueError):
        explain_external_executor(NodeArgs(period_ref="2025-Q1"), ctx, collector=lambda **kw: None)
