"""`EvidenceBundleV2` как адаптер (Срез 2, B2, docs/build-plan/12-srez-02-evidence-bundle-v2.md).

Definition of Done: «Один запуск даёт одну доказательную базу
(`EvidenceBundleV2`)» + «Существующие выгрузки (`metrics.json` и потребители)
работают без изменения пользовательского результата — обратная совместимость
подтверждена регрессионным сравнением (C7)».

Строки ниже — РЕАЛЬНАЯ форма конверта `{run, metrics, segments, group}`, как её
собирают `two_stage_extractor.py` (флаг `dimension` у обычной строки, ключи
`period_values`/`value_current`/`found`/`is_calculated`), `consolidated_group.py`
(`dimension="consolidated_group"`, `group_relation`/`group_registration`/
`group_activity`) и `ratios.py::compute_ratios` (`is_calculated=True`,
происхождение вторым каналом в `inputs`) — не выдуманная форма: `tests/golden/
extraction/` ещё пуст (PHASE-0-STATUS.md), поэтому фикстура собрана из чтения
самого движка, а не из golden-набора.
"""
from __future__ import annotations

import pytest

from finengine.contracts import EvidenceBundleV2
from finengine.skills.evidence_bundle import evidence_bundle_from_envelope, legacy_rows_from_bundle

RUN_ID = "run-abc123"


def _extracted_row(enum: str, *, cur: float, prev: float, unit: str = "млн руб.",
                    page: int = 5, dimension: str = "", segment: str = "") -> dict:
    row = {
        "type": "metric", "enum": enum, "name_ru": enum,
        "value_current": cur, "value_prev": prev,
        "period_values": [
            {"period": "2024", "value": cur, "unit": unit, "page": page, "source_text": ""},
            {"period": "2023", "value": prev, "unit": unit, "page": page, "source_text": ""},
        ],
        "unit": unit, "found": True, "is_calculated": False, "formula": "", "page": page,
    }
    if dimension:
        row.update(dimension=dimension, segment=segment or enum)
    return row


def _calculated_row(enum: str, *, cur: float, prev: float, inputs: list[str],
                    unit: str = "%") -> dict:
    return {
        "type": "metric", "enum": enum, "name_ru": enum,
        "value_current": cur, "value_prev": prev,
        "period_values": [
            {"period": "2024", "value": cur, "unit": unit, "page": None, "source_text": ""},
            {"period": "2023", "value": prev, "unit": unit, "page": None, "source_text": ""},
        ],
        "unit": unit, "found": True, "is_calculated": True,
        "formula": f"({'+'.join(inputs)})", "inputs": inputs,
    }


def _not_found_row(enum: str) -> dict:
    return {
        "type": "metric", "enum": enum, "name_ru": enum,
        "value_current": None, "value_prev": None, "period_values": [],
        "unit": "млн руб.", "found": False, "is_calculated": False, "formula": "", "page": None,
    }


def _group_row(name: str, *, relation: str) -> dict:
    return {
        "type": "metric", "enum": "CONSOLIDATED_GROUP_OWNERSHIP", "name_ru": name,
        "value_current": 100.0, "value_prev": 100.0,
        "period_values": [{"period": "2024", "value": 100.0, "unit": "%", "page": 12,
                           "source_text": ""}],
        "unit": "%", "found": True, "is_calculated": False, "formula": "", "page": 12,
        "segment": name, "dimension": "consolidated_group",
        "source_text": f"relation: {relation}", "group_relation": relation,
        "group_registration": "РФ", "group_activity": "торговля",
    }


def _envelope() -> dict:
    return {
        "run": {"cell": "ifrs_nf_ru", "main_period": "2024", "prev_period": "2023",
               "run_passport": {"run_id": RUN_ID}},
        "metrics": [
            _extracted_row("REVENUE", cur=1000.0, prev=900.0),
            _not_found_row("GOODWILL"),
            _calculated_row("ROE", cur=15.5, prev=14.2, inputs=["NET_INCOME", "EQUITY"]),
        ],
        "segments": [
            _extracted_row("REVENUE", cur=600.0, prev=550.0, page=8,
                           dimension="operating_segment", segment="Розница"),
        ],
        "group": [
            _group_row("ООО Дочка", relation="дочерняя компания"),
        ],
    }


# ── Контракт ─────────────────────────────────────────────────────────────

def test_bundle_is_frozen_and_requires_a_run_id():
    with pytest.raises(Exception, match="run_id"):
        EvidenceBundleV2()
    bundle = EvidenceBundleV2(run_id=RUN_ID)
    with pytest.raises(Exception, match="frozen"):
        bundle.run_id = "tampered"


# ── Извлечённые показатели → ScalarFact, по одному на период ──────────────

def test_an_extracted_metric_becomes_one_scalar_fact_per_period():
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    revenue = [f for f in bundle.scalar_facts if f.concept == "REVENUE"
               and f.source_ref.startswith("REVENUE@page:5")]
    assert {f.period_ref: f.value for f in revenue} == {"2024": 1000.0, "2023": 900.0}
    assert all(f.unit == "млн руб." and f.page == 5 for f in revenue)
    assert all(f.produced_by_atom == "two_stage_extractor" for f in revenue)


def test_a_row_not_found_produces_no_facts():
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    assert not any(f.concept == "GOODWILL" for f in bundle.scalar_facts)


def test_a_row_without_a_dimension_check_excludes_segment_rows_from_scalars():
    """Строка из `segments` НЕ должна попасть в плоские `scalar_facts` бандла —
    её место в `StructuredFact`, иначе показатель компании и показатель одного
    сегмента слились бы под одним `concept` без разбора (не тот показатель,
    что имел в виду решатель, поднимающий сумму по компании)."""
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    flat_revenue_values = {f.value for f in bundle.scalar_facts if f.concept == "REVENUE"}
    assert 600.0 not in flat_revenue_values and 550.0 not in flat_revenue_values


# ── Расчёт (formula_engine/compute_ratios) → DerivedFact с трассировкой ───

def test_a_calculated_row_becomes_a_derived_fact_with_its_inputs():
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    roe = {f.period_ref: f for f in bundle.derived_facts if f.concept == "ROE"}
    assert set(roe) == {"2024", "2023"}
    assert roe["2024"].value == 15.5
    assert roe["2024"].formula_id == "ROE"
    assert roe["2024"].inputs == ["NET_INCOME", "EQUITY"]
    assert roe["2024"].produced_by_atom == "formula_engine"
    assert not any(f.concept == "ROE" for f in bundle.scalar_facts), (
        "расчётный показатель не должен задваиваться в ScalarFact — ROE есть "
        "только в реестре ячейки формулой, извлечения по нему нет")


def test_derived_fact_inputs_survive_the_real_trace_shape():
    """ДЕФЕКТ, ПОЙМАННЫЙ ПРОГОНОМ НА ДЕВ-СТЕНДЕ (реальный `calc_run.py` +
    `compute_ratios` над своей формулой `DEP_TO_CASH`): `row["inputs"]` — не
    список голых имён, а список СЛОВАРЕЙ трассировки формулы, у каждого свои
    `enum`/`value`/`substituted_zero`/`transform`/`period`. Первая версия
    адаптера писала `str(словарь)` целиком в `DerivedFact.inputs`, и вместо
    `"BANK_DEPOSITS"` там оказывалась строка вида `"{'enum': 'BANK_DEPOSITS',
    'value': 60177.0, …}"` — бесполезная для сверки происхождения."""
    row = _calculated_row("DEP_TO_CASH", cur=1.31, prev=1.20, inputs=[], unit="")
    row["inputs"] = [
        {"enum": "BANK_DEPOSITS", "value": 60177.0, "substituted_zero": False,
         "transform": "", "period": "FY 2021"},
        {"enum": "CASH_EQUIVALENTS", "value": 45920.0, "substituted_zero": False,
         "transform": "", "period": "FY 2021"},
    ]
    envelope = {"metrics": [row], "segments": [], "group": []}
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    fact = next(f for f in bundle.derived_facts if f.period_ref == "2024")
    assert fact.inputs == ["BANK_DEPOSITS", "CASH_EQUIVALENTS"]


def test_a_dimensionless_calculated_row_still_becomes_a_derived_fact():
    """ДЕФЕКТ, ПОЙМАННЫЙ ПРОГОНОМ НА ДЕВ-СТЕНДЕ: своя формула с `unit_kind
    "ratio"` (`skills/ratios.py`) законно приходит БЕЗ единицы — `unit_for_
    calculated` не обязан подставлять что-то непустое для чистого отношения.
    `DerivedFact.unit` (A0) поэтому и объявлен необязательным (`str | None`),
    в отличие от `ScalarFact.unit`, — и отсутствие единицы не повод молчать
    расчётный факт: реальный прогон `DEP_TO_CASH = BANK_DEPOSITS / CASH_
    EQUIVALENTS` в `tests/test_evidence_bundle_reaches_the_envelope.py` терял
    факт целиком, пока это не было различено от отсутствия единицы у ScalarFact."""
    row = _calculated_row("PURE_RATIO", cur=1.31, prev=1.20, inputs=["A", "B"], unit="")
    envelope = {"metrics": [row], "segments": [], "group": []}
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    ratio = {f.period_ref: f for f in bundle.derived_facts if f.concept == "PURE_RATIO"}
    assert set(ratio) == {"2024", "2023"}
    assert ratio["2024"].value == 1.31
    assert ratio["2024"].unit is None


# ── Сегменты/группа → StructuredFact, по одному на вид таблицы ────────────

def test_segments_and_group_become_separate_structured_facts():
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    by_kind = {f.table_kind: f for f in bundle.structured_facts}
    assert set(by_kind) == {"segments", "consolidated_group"}
    assert len(by_kind["segments"].rows) == 1
    assert by_kind["segments"].rows[0]["segment"] == "Розница"
    assert by_kind["consolidated_group"].rows[0]["group_relation"] == "дочерняя компания"


def test_no_segments_or_group_produces_no_structured_facts():
    envelope = _envelope()
    envelope["segments"] = []
    envelope["group"] = []
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    assert bundle.structured_facts == []


# ── Один прогон — одна доказательная база: и извлечение, и расчёт в ОДНОМ бандле ──

def test_one_run_has_one_evidence_base_across_extraction_and_calc():
    """DoD Среза 2: строка `formula_engine` дописывается в ТОТ ЖЕ набор, что и
    извлечённые строки — не в параллельный. Проверяется тем, что ОДИН вызов
    адаптера над ОДНИМ конвертом (как он выглядит после calc_run.py) отдаёт
    оба вида фактов в одном `EvidenceBundleV2`, а не в двух объектах."""
    bundle = evidence_bundle_from_envelope(RUN_ID, _envelope())
    assert bundle.run_id == RUN_ID
    assert bundle.scalar_facts and bundle.derived_facts and bundle.structured_facts


# ── Живучесть адаптера: кривая строка не роняет сборку ─────────────────────

@pytest.mark.parametrize("bad_row", [
    {},  # нет enum
    {"enum": "X", "found": True},  # нет unit/period_values/value_current
    {"enum": "X", "found": True, "value_current": 1.0, "unit": "", "period_values": []},
    "не словарь",
    None,
])
def test_a_malformed_row_is_skipped_not_raised(bad_row):
    envelope = {"metrics": [bad_row], "segments": [], "group": []}
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    assert bundle.scalar_facts == [] and bundle.derived_facts == []


def test_an_empty_envelope_produces_an_empty_but_valid_bundle():
    bundle = evidence_bundle_from_envelope(RUN_ID, {})
    assert bundle.scalar_facts == bundle.structured_facts == bundle.derived_facts == []


# ── Обратная совместимость: числа восстанавливаются по бандлу (C7) ────────

def test_legacy_rows_from_bundle_round_trips_the_numbers():
    envelope = _envelope()
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    legacy = legacy_rows_from_bundle(bundle, main_period="2024", prev_period="2023")

    by_enum = {r["enum"]: r for r in legacy["metrics"]}
    assert by_enum["REVENUE"]["value_current"] == 1000.0
    assert by_enum["REVENUE"]["value_prev"] == 900.0
    assert by_enum["REVENUE"]["unit"] == "млн руб."
    assert by_enum["REVENUE"]["is_calculated"] is False

    assert by_enum["ROE"]["value_current"] == 15.5
    assert by_enum["ROE"]["value_prev"] == 14.2
    assert by_enum["ROE"]["is_calculated"] is True

    assert "GOODWILL" not in by_enum  # не найден — фактов нет, строки нет

    assert legacy["segments"][0]["segment"] == "Розница"
    assert legacy["group"][0]["group_relation"] == "дочерняя компания"


def test_legacy_rows_use_none_for_a_period_without_a_matching_fact():
    """Реконструкция смотрит по МЕТКАМ периода, а не угадывает: если попросить
    несуществующий ярлык периода, значение — пробел, а не число другого года."""
    envelope = _envelope()
    bundle = evidence_bundle_from_envelope(RUN_ID, envelope)
    legacy = legacy_rows_from_bundle(bundle, main_period="2099", prev_period="2023")
    by_enum = {r["enum"]: r for r in legacy["metrics"]}
    assert by_enum["REVENUE"]["value_current"] is None
    assert by_enum["REVENUE"]["value_prev"] == 900.0
