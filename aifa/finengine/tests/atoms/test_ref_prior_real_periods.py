"""`ref.prior` на РЕАЛЬНЫХ подписях периодов (Срез 32, живой прогон 25.09.2026).

На РСБУ-отчёте ЛУКОЙЛ пара уровня прогона (`_select_run_periods`) дала `FY 2025 →
31.12.2024` («предыдущий» — дата), а факты интервальных показателей подписаны
`FY 2025/FY 2024`, баланса — `31.12.2025/2024/2023`. Пара прогона не совпала ни с одним
показателем, и сравнение не нашлось нигде. Критерии:

1. Пара прогона не совпала → пара строится по подписям самого показателя.
2. Интервал: `FY 2025 → FY 2024`; баланс: последняя дата и та же дата годом раньше;
   промежуточный баланс (`30.06.2026`) → `31.12.2025`.
3. Пропуск года не сравнивается, а не угадывается; явно заданный период не переопределяется;
   несопоставимый базис по-прежнему даёт `not_comparable`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from finengine.contracts import (
    ClassificationResult,
    EntityIdentity,
    ExtractedMetric,
    ReferenceValue,
    RunPassport,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.atoms.ref_prior import _comparable_pair, ref_prior_executor
from finengine.skills.spec.atom_registry import NodeArgs


def _passport() -> RunPassport:
    return RunPassport(
        goal_ir_id="goal-1",
        classification=ClassificationResult(
            profile_id="rsbu_nf_ru", standard="rsbu", company_type="non_financial", language="ru",
            reason="test fixture",
        ),
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version="registry-7", methodology_version="methodology-1",
        model_version="model-1", prompt_version="prompt-1", sandbox_image_version="sandbox-1",
    )


def _metric(period: str, value: float, *, basis: str | None = None) -> ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref=f"loc:{period}",
        concept="INVENTORIES", period_ref=period, value=value, unit="млн. руб.", basis=basis,
    )
    return ExtractedMetric(scalar_fact=fact, concept="INVENTORIES", period_ref=period)


@dataclass
class _Inputs:
    document_text: str = "текст отчёта"


@dataclass
class _Ctx:
    _deps: list
    run_passport: RunPassport | None = field(default_factory=_passport)
    run_id: str = "run-1"
    inputs: _Inputs = field(default_factory=_Inputs)

    def dependencies(self):
        return tuple(self._deps)


@pytest.fixture
def lukoil_detector(monkeypatch):
    """Пара уровня прогона, которую детектор вернул на реальном РСБУ-отчёте."""
    from finengine.skills import bulk_extractor as bulk

    monkeypatch.setattr(
        bulk, "_select_run_periods",
        lambda text, doc_period="", **_kw: (["FY 2025", "31.12.2024", "31.12.2023"], "FY 2025", "31.12.2024"),
    )


def _reference(metrics, **node) -> ReferenceValue | list:
    result = ref_prior_executor(NodeArgs(concept="INVENTORIES", **node), _Ctx(list(metrics)))
    return result[0] if len(result) == 1 else result


def test_interval_facts_pair_as_fy_to_fy_although_the_run_pair_named_a_date(lukoil_detector):
    cur, old = _metric("FY 2025", 1500.0), _metric("FY 2024", 1200.0)

    ref = _reference([cur, old])

    assert isinstance(ref, ReferenceValue)
    assert (ref.current_period_ref, ref.prior_period_ref) == ("FY 2025", "FY 2024")
    assert (ref.current_evidence_id, ref.prior_evidence_id) == (cur.scalar_fact.evidence_id, old.scalar_fact.evidence_id)
    assert "подписями периодов самого показателя" in ref.selection_reason


def test_balance_facts_pair_the_latest_date_with_the_same_date_a_year_earlier(lukoil_detector):
    facts = [_metric("31.12.2023", 900.0), _metric("31.12.2025", 1500.0), _metric("31.12.2024", 1200.0)]

    ref = _reference(facts)

    assert isinstance(ref, ReferenceValue)
    assert (ref.current_period_ref, ref.prior_period_ref) == ("31.12.2025", "31.12.2024"), (
        "сравнение — с ближайшим предыдущим годом, а не с самым старым значением"
    )


def test_an_interim_balance_date_is_compared_with_the_previous_year_end(monkeypatch):
    from finengine.skills import bulk_extractor as bulk

    monkeypatch.setattr(
        bulk, "_select_run_periods",
        lambda text, doc_period="", **_kw: (["6M 2026", "6M 2025", "30.06.2026"], "6M 2026", "6M 2025"),
    )
    facts = [_metric("30.06.2026", 1500.0), _metric("31.12.2025", 1400.0), _metric("31.12.2024", 1100.0)]

    ref = _reference(facts)

    assert isinstance(ref, ReferenceValue)
    assert (ref.current_period_ref, ref.prior_period_ref) == ("30.06.2026", "31.12.2025")


def test_an_inherited_interim_run_period_is_coerced_to_the_point_metric_axis(monkeypatch):
    """Root period 6M 2026 describes the report, not every metric's axis.

    Balance metrics use the same report end as a point date.  Treating the
    inherited root period as an explicit metric pin used to suppress the
    otherwise valid 30.06.2026 -> 31.12.2025 pair.
    """
    from finengine.skills import bulk_extractor as bulk

    monkeypatch.setattr(
        bulk, "_select_run_periods",
        lambda text, doc_period="", **_kw: (["6M 2026", "6M 2025"], "6M 2026", "6M 2025"),
    )
    facts = [_metric("30.06.2026", 1500.0), _metric("31.12.2025", 1400.0)]

    ref = _reference(facts, period_ref="6M 2026")

    assert isinstance(ref, ReferenceValue)
    assert (ref.current_period_ref, ref.prior_period_ref) == ("30.06.2026", "31.12.2025")


def test_a_skipped_year_is_not_compared_instead_of_guessed(lukoil_detector):
    assert _reference([_metric("FY 2025", 1500.0), _metric("FY 2023", 1000.0)]) == []
    assert _reference([_metric("31.12.2025", 1500.0), _metric("31.12.2023", 1000.0)]) == []


def test_an_explicitly_pinned_period_is_never_overridden_by_the_concept_pair(lukoil_detector):
    facts = [_metric("FY 2025", 1500.0), _metric("FY 2024", 1200.0)]

    assert _reference(facts, period_ref="FY 2024") == []


def test_the_run_pair_still_wins_when_it_matches_the_concept(monkeypatch):
    from finengine.skills import bulk_extractor as bulk

    monkeypatch.setattr(
        bulk, "_select_run_periods",
        lambda text, doc_period="", **_kw: (["FY 2024", "FY 2023"], "FY 2024", "FY 2023"),
    )
    facts = [_metric("FY 2025", 1600.0), _metric("FY 2024", 1500.0), _metric("FY 2023", 1200.0)]

    ref = _reference(facts)

    assert isinstance(ref, ReferenceValue)
    assert (ref.current_period_ref, ref.prior_period_ref) == ("FY 2024", "FY 2023")
    assert "детектором периодов" in ref.selection_reason


def test_an_incomparable_basis_is_still_reported_on_the_concept_pair(lukoil_detector):
    result = _reference([_metric("FY 2025", 1500.0, basis="ifrs"), _metric("FY 2024", 1200.0, basis="rsbu")])

    assert isinstance(result, VerificationFinding) and result.status == "not_comparable"
    assert result.check_kind == "basis"


@pytest.mark.parametrize(("labels", "expected"), [
    (["FY 2025", "FY 2024"], ("FY 2025", "FY 2024")),
    (["FY 2024", "FY 2025", "FY 2023"], ("FY 2025", "FY 2024")),
    (["31.12.2025", "31.12.2024", "31.12.2023"], ("31.12.2025", "31.12.2024")),
    (["6M 2026", "6M 2025"], ("6M 2026", "6M 2025")),
    (["30.06.2026", "31.12.2025"], ("30.06.2026", "31.12.2025")),
    (["31.12.2025", "31.12.2023"], None),
    (["FY 2025"], None),
    (["FY 2025", ""], None),
    ([], None),
])
def test_comparable_pair_is_the_latest_period_and_its_exact_predecessor(labels, expected):
    assert _comparable_pair(labels) == expected
