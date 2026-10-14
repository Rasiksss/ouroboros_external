"""`read.structure` — критерий готовности атома (docs/build-plan/
15b-srez-05b-read-structure-quote-entities.md, карточка `read.structure`):

1. На размеченных строках структура извлекается с правильными ключами,
   периодами и значениями (замер на golden-наборе — вне этой сессии; здесь —
   точные примеры того же свойства).
2. Порядок/иерархия сохраняется там, где она значима — не сворачивается без
   правила (проверяется через сохранение отдельных измерений/меток).
3. `TOTAL`, элиминации и явные строки сохраняются, если присутствуют в
   источнике, а не сливаются молча.
4. Числа внутри структуры имеют то же происхождение/запрет на выдумывание,
   что `ScalarFact` payload — отсутствующий ряд не создаёт значение.

Единственная модельная граница (`_call_json_llm`, ДВА физических вызова:
compression + extraction) подменяется дублем — тот же приём, каким
пользуются `test_locate.py`/`test_read_value.py`.
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import StructuredFact
from finengine.skills import two_stage_extractor
from finengine.skills.atoms.read_structure import SegmentResponseInvalid, StructureSpec, read_structure_batch

_METRICS = [
    {"enum": "SALES_REVENUE", "name_en": "Revenue", "name_ru": "Выручка"},
    {"enum": "OPERATING_PROFIT", "name_en": "Operating profit", "name_ru": "Операционная прибыль"},
]


def _patch_call_json_llm(monkeypatch, *, compression=None, extraction=None):
    calls: list[dict] = []
    compression = compression if compression is not None else {"metrics": {}}
    extraction = extraction if extraction is not None else {"metrics": {}}

    async def _fake(**kwargs):
        calls.append(kwargs)
        payload = compression if kwargs.get("stage") == "read_structure_compression" else extraction
        return payload, {"stage": kwargs.get("stage")}, json.dumps(payload)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    return calls


def _run(monkeypatch, *, target_metrics, extraction, document_text="===PAGE 1===\nx"):
    _patch_call_json_llm(monkeypatch, extraction=extraction)
    return read_structure_batch(
        [StructureSpec(table_kind="segments", target_metrics=target_metrics)],
        document_id="doc-1", document_version="v1", document_text=document_text,
        period_current="2024", period_prev="2023",
    )


def test_table_kind_without_target_metrics_is_not_asked(monkeypatch):
    calls = _patch_call_json_llm(monkeypatch)
    result = read_structure_batch(
        [StructureSpec(table_kind="segments", target_metrics=[])],
        document_id="doc-1", document_version="v1", document_text="===PAGE 1===\nx",
        period_current="2024", period_prev="2023",
    )
    assert result == {"segments": []}
    assert calls == [], "table_kind без единого запрошенного показателя не должен уходить к модели"


def test_two_physical_calls_for_the_whole_batch(monkeypatch):
    extraction = {"metrics": {
        "SALES_REVENUE": {"rows": [
            {"segment": "Retail", "dimension": "operating_segment",
             "values": {"2024": "600"}, "unit_text": "RUB", "page": 8},
        ]},
        "OPERATING_PROFIT": {"rows": []},
    }}
    calls = _patch_call_json_llm(monkeypatch, extraction=extraction)
    result = read_structure_batch(
        [StructureSpec(table_kind="segments", target_metrics=_METRICS)],
        document_id="doc-1", document_version="v1", document_text="===PAGE 8===\n| Retail | 600 |",
        period_current="2024", period_prev="2023",
    )
    assert len(calls) == 2, "read.structure обязан сделать РОВНО два вызова (compression+extraction) на весь батч"
    assert {c["stage"] for c in calls} == {"read_structure_compression", "read_structure_extraction"}
    facts = result["segments"]
    assert len(facts) == 1
    fact = facts[0]
    assert isinstance(fact, StructuredFact)
    assert fact.table_kind == "segments"
    assert fact.produced_by_atom == "read.structure"


def test_row_carries_dimensions_concept_value_unit_period_page(monkeypatch):
    extraction = {"metrics": {"SALES_REVENUE": {"rows": [
        {"segment": "Retail", "dimension": "operating_segment",
         "values": {"2024": "600", "2023": "550"}, "unit_text": "RUB", "page": 8},
    ]}}}
    result = _run(monkeypatch, target_metrics=[_METRICS[0]], extraction=extraction)
    rows = result["segments"][0].rows
    by_period = {r["period_ref"]: r for r in rows}
    assert len(by_period) == 2
    row = next(r for r in rows if r["value"] == pytest.approx(600.0))
    assert row["dimensions"] == {"operating_segment": "Retail"}
    assert row["concept"] == "SALES_REVENUE"
    assert row["value"] == pytest.approx(600.0)
    assert row["unit"]
    assert row["page"] == 8
    assert row["is_total"] is False
    assert row["is_elimination"] is False


def test_total_row_is_kept_and_flagged_not_merged_away(monkeypatch):
    extraction = {"metrics": {"SALES_REVENUE": {"rows": [
        {"segment": "Retail", "dimension": "operating_segment",
         "values": {"2024": "600"}, "unit_text": "RUB", "page": 8},
        {"segment": "Итого", "dimension": "operating_segment",
         "values": {"2024": "1000"}, "unit_text": "RUB", "page": 8},
    ]}}}
    result = _run(monkeypatch, target_metrics=[_METRICS[0]], extraction=extraction)
    rows = result["segments"][0].rows
    total_rows = [r for r in rows if r["is_total"]]
    assert len(total_rows) == 1
    assert total_rows[0]["value"] == pytest.approx(1000.0)
    assert total_rows[0]["dimensions"] == {"operating_segment": "TOTAL"}
    assert total_rows[0]["raw_label"] == "Итого"
    non_total = [r for r in rows if not r["is_total"]]
    assert len(non_total) == 1 and non_total[0]["value"] == pytest.approx(600.0)


def test_elimination_row_is_flagged(monkeypatch):
    extraction = {"metrics": {"SALES_REVENUE": {"rows": [
        {"segment": "Corporate and eliminations", "dimension": "operating_segment",
         "values": {"2024": "-50"}, "unit_text": "RUB", "page": 9},
    ]}}}
    result = _run(monkeypatch, target_metrics=[_METRICS[0]], extraction=extraction)
    rows = result["segments"][0].rows
    assert len(rows) == 1
    assert rows[0]["is_elimination"] is True
    assert rows[0]["value"] == pytest.approx(-50.0)


def test_invalid_extraction_response_raises_for_the_executor_to_retry(monkeypatch):
    with pytest.raises(SegmentResponseInvalid):
        _run(monkeypatch, target_metrics=[_METRICS[0]], extraction={"metrics": {}})


def test_an_answer_saying_there_are_no_segment_rows_is_an_empty_result_not_a_failure(monkeypatch):
    """Срез 30, real-model smoke (Северсталь Q1-2025, FULL_ANALYSIS): промежуточный
    отчёт не раскрывает сегменты, и модель честно отвечает `{"rows": null}` /
    `{"rows": ""}`. Валидатор требовал list, ответ отвергался, узел падал после
    ретраев и блокировал 160 зависимых узлов вместе с отчётом. «Сегментов нет» —
    это пустой результат (как «показатель не нашёлся» у read.value), а не сбой."""
    extraction = {"metrics": {"SALES_REVENUE": {"rows": None}, "OPERATING_PROFIT": {"rows": ""}}}

    result = _run(monkeypatch, target_metrics=_METRICS, extraction=extraction)

    assert not result["segments"]


def test_one_row_crash_does_not_lose_the_others(monkeypatch):
    from finengine.skills import bulk_extractor as bulk

    extraction = {"metrics": {"SALES_REVENUE": {"rows": [
        {"segment": "Retail", "dimension": "operating_segment",
         "values": {"2024": "600"}, "unit_text": "RUB", "page": 8},
        {"segment": "Wholesale", "dimension": "operating_segment",
         "values": {"2024": "400"}, "unit_text": "RUB", "page": 8},
    ]}}}
    original = bulk._coerce_period_values

    def flaky(info, **kwargs):
        if info.get("segment") == "Wholesale":
            raise RuntimeError("сбой разбора именно этой строки")
        return original(info, **kwargs)
    monkeypatch.setattr(bulk, "_coerce_period_values", flaky)

    result = _run(monkeypatch, target_metrics=[_METRICS[0]], extraction=extraction)
    rows = result["segments"][0].rows
    assert len(rows) == 1
    assert rows[0]["dimensions"] == {"operating_segment": "Retail"}


def test_empty_rows_produce_no_structured_fact_not_an_empty_one(monkeypatch):
    result = _run(monkeypatch, target_metrics=[_METRICS[0]], extraction={"metrics": {"SALES_REVENUE": {"rows": []}}})
    assert result["segments"] == []


def test_maturity_open_bucket_is_representable_by_the_general_row_schema():
    """Нет готового extractor'а для сроков погашения в репозитории (проверено
    поиском по maturity/aging/sensitivit — только упоминания в списках
    исключений «не финансовый показатель», не производитель) — этот тест
    проверяет ТОЛЬКО форму строки, уже используемую сегментами: открытый
    интервал «5+ лет» должен уместиться как обычная координата измерения,
    а не как отдельный код показателя (карточка: «не превращай каждую
    combination в отдельный metric code»)."""
    fact = StructuredFact(
        produced_by_atom="read.structure", produced_by_atom_version="1.0.0",
        source_ref="doc-1#maturity", table_kind="maturity",
        rows=[{
            "dimensions": {"maturity_bucket": "5+ years"}, "concept": "FINANCIAL_LIABILITIES",
            "value": 1200.0, "unit": "RUB_MLN", "period_ref": "2024", "page": 40,
            "is_total": False, "is_elimination": False, "is_open_ended": True, "raw_label": "5+ years",
        }],
    )
    assert fact.rows[0]["is_open_ended"] is True
    assert fact.rows[0]["dimensions"]["maturity_bucket"] == "5+ years"
