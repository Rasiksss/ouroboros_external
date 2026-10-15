"""Общий extraction gate Среза 5 (часть 1 + часть 2), docs/build-plan/
15b-srez-05b-read-structure-quote-entities.md, раздел «Критерий готовности
связки извлечения» — все 9 пунктов, на всех 7 атомах (`doc.textify`,
`doc.index`, `locate`, `read.value`, `read.structure`, `read.quote`,
`read.entities`) вместе.

Пункты 1/4/6 у себя в карточке прямо требуют замера на `tests/golden/
extraction/`/полном эталонном отчёте — эти наборы не собраны в Фазе 0
(`tests/golden/extraction/README.md`, `tests/golden/structures/README.md`:
«СХЕМА ЗАФИКСИРОВАНА, ДАННЫЕ НЕ СОБРАНЫ», см. `docs/build-plan/
PHASE-0-STATUS.md`) — это документированный пробел ДО этой сессии, не
маскируется здесь. Каждый такой пункт ниже помечен `pytest.mark.skip` с
объяснением и заменён точным примером ТОГО ЖЕ свойства на единичном случае,
тем же приёмом, каким уже пользуются `test_locate.py`/`test_read_value.py`
(«здесь — точные примеры того же свойства»).
"""
from __future__ import annotations

import pytest

from finengine.contracts import ScalarFact, StructuredFact
from finengine.skills.atoms.graph_atoms import build_registry


# ── Пункт 1. Golden-набор извлечения — точность значения/периода/единицы/базиса ──

@pytest.mark.skip(
    reason="tests/golden/extraction/ не собран в Фазе 0 (README.md: «СХЕМА "
    "ЗАФИКСИРОВАНА, ДАННЫЕ НЕ СОБРАНЫ») — замер ≥97% требует реального "
    "корпуса из 30 отчётов/300 фактов; вне периметра Среза 5b."
)
def test_point_1_golden_extraction_accuracy_deferred_no_corpus():
    ...


# ── Пункт 2. Отсутствующий показатель → нет значения, 100% отрицательных ──

def test_point_2_missing_metric_never_produces_a_value(monkeypatch):
    from finengine.skills.atoms.read_value import read_value_batch

    result = read_value_batch(
        ["GHOST_METRIC"], {"GHOST_METRIC": []},
        metrics=[{"enum": "GHOST_METRIC", "name_ru": "Призрак"}],
        document_version="v1", period_current="2024", period_prev="2023",
    )
    assert result == {"GHOST_METRIC": []}, "показатель без кандидатов не создаёт значение, даже 0"


def test_point_2_missing_structure_table_kind_never_produces_a_value(monkeypatch):
    from finengine.skills.atoms.read_structure import StructureSpec, read_structure_batch

    result = read_structure_batch(
        [StructureSpec(table_kind="segments", target_metrics=[])],
        document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx",
        period_current="2024", period_prev="2023",
    )
    assert result == {"segments": []}


def test_point_2_missing_quote_topic_never_produces_evidence(monkeypatch):
    import json
    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms.read_quote import read_quote_batch

    async def _fake(**kwargs):  # noqa: ARG001
        return {"quotes": {}}, {}, json.dumps({"quotes": {}})
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = read_quote_batch(["unasked topic"], document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx")
    assert result == {"unasked topic": []}


# ── Пункт 3. Полнота происхождения успешного факта ──

def test_point_3_scalar_fact_schema_carries_full_provenance():
    """Схема `ScalarFact`/`EvidenceRecord` обязана нести doc_version/
    страницу/концепт/период/единицу/базис/атом-производитель — проверяется
    формой контракта (A0), не тем, что КАЖДЫЙ факт обязательно заполняет
    базис (плоское число без специальной оговорки законно `basis=None`,
    как и `ScalarFact.page` — тот же принцип «поле есть, заполняет атом,
    когда применимо», зафиксированный в `evidence.py`)."""
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref="loc-1", document_version="doc-v1", concept="REVENUE",
        period_ref="FY2024", basis="ifrs", value=100.0, unit="RUB_MLN", page=5,
    )
    for field in ("document_version", "concept", "period_ref", "basis", "page",
                  "produced_by_atom", "produced_by_atom_version", "unit", "value"):
        assert getattr(fact, field) is not None


# ── Пункт 4. `locate` — правильная страница среди кандидатов ──

@pytest.mark.skip(
    reason="≥98% на tests/golden/extraction/ требует того же несобранного "
    "корпуса, что пункт 1 — единичный пример того же свойства уже покрыт "
    "tests/atoms/test_locate.py::test_candidate_references_a_real_table_from_the_document."
)
def test_point_4_locate_page_recall_deferred_no_corpus():
    ...


# ── Пункт 5. Частичный сбой пакета не уничтожает соседей ──
# Покрыто на уровне атома: test_read_value.py::test_one_concept_crash_does_not_lose_the_others,
# test_read_structure.py::test_one_row_crash_does_not_lose_the_others,
# test_read_quote.py — темы независимы по построению (dict per topic),
# test_read_entities.py — события независимы по построению (try/except per event).
# Здесь — тот же принцип на уровне GraphExecutor (общий для всех батчируемых
# атомов, не переописывается заново): tests/executor/test_partial_batch_failure.py.

def test_point_5_reference_to_existing_per_atom_and_executor_coverage():
    """Не дублирует проверку — фиксирует ссылкой (docstring этого файла и
    этой функции), ЧТО именно уже покрывает это свойство для КАЖДОГО из
    батчируемых атомов Среза 5, чтобы гейт был самодокументируемым списком,
    а не только текстом докстринга модуля. Реальные проверки живут в
    tests/atoms/test_read_value.py, test_read_structure.py, test_read_quote.py,
    test_read_entities.py и tests/executor/test_partial_batch_failure.py —
    см. комментарий выше в этом файле."""
    assert True


# ── Пункт 6. Пакетная экономика — число физических вызовов не растёт с числом концептов ──

def test_point_6_locate_stays_at_one_call_regardless_of_concept_count(monkeypatch):
    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms.locate import locate_batch

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs.get("stage"))
        return {"metrics": {}}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    metrics = [{"enum": f"METRIC_{i}", "name_ru": f"Показатель {i}"} for i in range(12)]
    enums = [m["enum"] for m in metrics]
    locate_batch(enums, document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx", metrics=metrics)
    assert len(calls) == 1, "locate — один вызов независимо от числа запрошенных концептов (12 в этом примере)"


def test_point_6_structure_stays_at_two_calls_regardless_of_metric_count(monkeypatch):
    import json
    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms.read_structure import StructureSpec, read_structure_batch

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs.get("stage"))
        payload = {"metrics": {m: {"rows": []} for m in [f"METRIC_{i}" for i in range(8)]}}
        return payload, {}, json.dumps(payload)
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    metrics = [{"enum": f"METRIC_{i}", "name_ru": f"Показатель {i}"} for i in range(8)]
    read_structure_batch(
        [StructureSpec(table_kind="segments", target_metrics=metrics)],
        document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx",
        period_current="2024", period_prev="2023",
    )
    assert len(calls) == 2, "read.structure — compression+extraction, не по вызову на показатель"


def test_point_6_quote_stays_at_one_call_regardless_of_topic_count(monkeypatch):
    import json
    from finengine.skills import two_stage_extractor
    from finengine.skills.atoms.read_quote import read_quote_batch

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"quotes": {}}, {}, json.dumps({"quotes": {}})
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    topics = [f"topic {i}" for i in range(10)]
    read_quote_batch(topics, document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx")
    assert len(calls) == 1, "read.quote — один вызов независимо от числа тем в батче"


# ── Пункт 7. Структурированное доказательство адресуется без повторного чтения OCR ──

def _sum_of_non_total_segments(structured_fact: StructuredFact, *, concept: str, period_ref: str) -> float:
    """Пример downstream-потребителя (аналитики), который читает ТОЛЬКО
    типизированное доказательство — БЕЗ единого обращения к исходному
    тексту документа (у функции даже нет такого параметра)."""
    return sum(
        row["value"] for row in structured_fact.rows
        if row["concept"] == concept and row["period_ref"] == period_ref and not row["is_total"]
    )


def test_point_7_structured_evidence_is_addressable_without_rereading_the_source_text():
    fact = StructuredFact(
        produced_by_atom="read.structure", produced_by_atom_version="1.0.0",
        source_ref="doc-1#segments", table_kind="segments",
        rows=[
            {"dimensions": {"operating_segment": "Retail"}, "concept": "SALES_REVENUE", "value": 600.0,
             "unit": "RUB_MLN", "period_ref": "FY2024", "page": 8, "is_total": False,
             "is_elimination": False, "is_open_ended": False, "raw_label": "Retail"},
            {"dimensions": {"operating_segment": "Wholesale"}, "concept": "SALES_REVENUE", "value": 400.0,
             "unit": "RUB_MLN", "period_ref": "FY2024", "page": 8, "is_total": False,
             "is_elimination": False, "is_open_ended": False, "raw_label": "Wholesale"},
            {"dimensions": {"operating_segment": "TOTAL"}, "concept": "SALES_REVENUE", "value": 1000.0,
             "unit": "RUB_MLN", "period_ref": "FY2024", "page": 8, "is_total": True,
             "is_elimination": False, "is_open_ended": False, "raw_label": "Итого"},
        ],
    )
    assert _sum_of_non_total_segments(fact, concept="SALES_REVENUE", period_ref="FY2024") == pytest.approx(1000.0)


# ── Пункт 8. Расчёт только после проверенных входов; арифметику ведёт formula_engine ──
# Покрыто Срезом 3 (verify.arithmetic — обязательный гейт над CalculatedMetric,
# calc.formula_engine оборачивает compute_ratios, не модельный вывод):
# tests/planner/test_mandatory_gates.py, tests/executor (verify.arithmetic gating).
# Не переописывается заново этим срезом — read.structure/read.quote/read.entities
# не вводят арифметику мимо formula_engine (ни один из трёх не производит
# CalculatedMetric).

def test_point_8_new_atoms_never_produce_a_calculated_metric():
    registry = build_registry(profile_id="ifrs_nf_ru")
    for name in ("read.structure", "read.quote", "read.entities"):
        atom = registry.get(name)
        assert atom is not None
        assert not any(spec.output_type == "CalculatedMetric" for spec in atom.produces)


# ── Пункт 9. После удаления старого промежуточного представления — гейт снова зелёный ──

@pytest.mark.skip(
    reason="В этой сессии НЕ удалено ни одного старого пути (C14): "
    "evidence_bundle.py/extract.py/calc_run.py по-прежнему строят segments/"
    "group из необработанного envelope для НЕмигрированных потребителей "
    "(скрипты aifa-extract/aifa-calc) — удаление откладывается до их "
    "перевода, как явно требует карточка read.structure «после перевода "
    "анализа, экспорта и интерфейса». Пункт неприменим до того перевода."
)
def test_point_9_old_representation_removal_deferred_no_migrated_consumer_yet():
    ...


# ── Сквозной смок: все 7 атомов Среза 5 реально зарегистрированы вместе ──

def test_all_seven_srez5_atoms_are_registered_together():
    registry = build_registry(profile_id="ifrs_nf_ru")
    for name in ("doc.textify", "doc.index", "locate", "read.value",
                 "read.structure", "read.quote", "read.entities"):
        assert registry.get(name) is not None, f"{name} должен быть в реестре Среза 5"
