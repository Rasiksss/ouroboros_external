"""`read.condition` — критерий готовности атома (docs/build-plan/
19-srez-09-dogovory-normativy-metodiki.md, карточка `read.condition`):

1. Каждое принятое условие содержит точную цитату/страницу и структурированные
   параметры, необходимые для последующего сравнения.
2. Оператор и порог разбираются детерминированно в закрытое перечисление/число.
3. Неясное или неполное условие не превращается в точное числовое правило —
   остаётся неполным доказательством (`operator`/`threshold_value` = `None`).
4. Атом не оценивает соблюдение условия — контракт не несёт вердикта вовсе.
5. Неверный адрес (страница/цитата не совпадают с документом) блокируется —
   `ConditionEvidence` не создаётся.
6. Несколько условий на один показатель (несколько ковенантов) сохраняются
   как отдельные записи, не сворачиваются в одну.
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import ConditionEvidence
from finengine.skills import two_stage_extractor
from finengine.skills.atoms.read_condition import (
    ConditionResponseInvalid,
    parse_operator,
    parse_threshold_value,
    read_condition_batch,
)

_DOCUMENT_TEXT = (
    "===PAGE 1===\n"
    "Definitions apply throughout this Agreement.\n"
    "===PAGE 5===\n"
    "The Borrower shall ensure that the ratio of Net Debt to EBITDA does not exceed 3.5x, "
    "tested quarterly, excluding lease liabilities under IFRS 16.\n"
    "===PAGE 6===\n"
    "The Borrower shall maintain a Current Ratio of at least 1.2, tested annually.\n"
    "The Guarantor shall maintain a Current Ratio of at least 1.0, tested annually.\n"
)


def _patch_call_json_llm(monkeypatch, response):
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    return calls


# ---------------------------------------------------------------------------
# Детерминированный разбор оператора/порога — чистые функции.
@pytest.mark.parametrize("text,expected", [
    ("does not exceed", "<="), ("not to exceed", "<="), ("no more than", "<="), ("не более", "<="),
    ("не должен превышать", "<="),
    ("at least", ">="), ("no less than", ">="), ("не менее", ">="),
    ("less than", "<"), ("менее", "<"),
    ("greater than", ">"), ("превышает", ">"),
    ("equal to", "=="), ("равен", "=="),
    ("not equal", "!="), ("не равен", "!="),
    ("совершенно неизвестная формулировка", None),
    ("", None),
])
def test_operator_parsing_is_deterministic_and_closed(text, expected):
    assert parse_operator(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("3.5x", 3.5), ("3,5", 3.5), ("at least 1.2", 1.2), ("-0.5", -0.5), ("", None), ("no digits here", None),
])
def test_threshold_parsing_is_deterministic(text, expected):
    assert parse_threshold_value(text) == expected


# ---------------------------------------------------------------------------
# Граф-уровень: read_condition_batch.
def test_topic_without_text_is_not_asked(monkeypatch):
    calls = _patch_call_json_llm(monkeypatch, {"conditions": {}})
    result = read_condition_batch([""], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT)
    assert result == {"": []}
    assert calls == [], "тема без непустой строки не должна доходить до модели"


def test_one_physical_call_for_the_whole_batch(monkeypatch):
    response = {"conditions": {
        "RATIO_NET_DEBT_EBITDA": [{
            "page": 5,
            "quote": (
                "The Borrower shall ensure that the ratio of Net Debt to EBITDA does not exceed 3.5x, "
                "tested quarterly, excluding lease liabilities under IFRS 16."
            ),
            "operator_text": "does not exceed", "threshold_text": "3.5x", "unit_text": "x",
            "subject": "the Borrower",
            "definition_text": "Net Debt to EBITDA, excluding lease liabilities under IFRS 16",
            "exceptions": ["excluding lease liabilities under IFRS 16"],
            "effective_period": "quarterly",
        }],
        "CURRENT_RATIO": [{
            "page": 6, "quote": "The Borrower shall maintain a Current Ratio of at least 1.2, tested annually.",
            "operator_text": "at least", "threshold_text": "1.2", "subject": "the Borrower",
        }],
    }}
    calls = _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["RATIO_NET_DEBT_EBITDA", "CURRENT_RATIO"],
        document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert len(calls) == 1, "read.condition обязан спросить модель ОДИН раз на весь батч тем"
    assert set(result) == {"RATIO_NET_DEBT_EBITDA", "CURRENT_RATIO"}
    assert len(result["RATIO_NET_DEBT_EBITDA"]) == 1
    assert len(result["CURRENT_RATIO"]) == 1


def test_full_structured_condition_is_extracted_with_provenance(monkeypatch):
    response = {"conditions": {"RATIO_NET_DEBT_EBITDA": [{
        "page": 5,
        "quote": (
            "The Borrower shall ensure that the ratio of Net Debt to EBITDA does not exceed 3.5x, "
            "tested quarterly, excluding lease liabilities under IFRS 16."
        ),
        "operator_text": "does not exceed", "threshold_text": "3.5x", "unit_text": "x",
        "subject": "the Borrower",
        "definition_text": "Net Debt to EBITDA, excluding lease liabilities under IFRS 16",
        "exceptions": ["excluding lease liabilities under IFRS 16"],
        "effective_period": "quarterly",
    }]}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["RATIO_NET_DEBT_EBITDA"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    evidence = result["RATIO_NET_DEBT_EBITDA"][0]
    assert isinstance(evidence, ConditionEvidence)
    assert evidence.page == 5
    assert evidence.operator == "<="
    assert evidence.threshold_value == 3.5
    assert evidence.unit == "x"
    assert evidence.subject == "the Borrower"
    assert evidence.definition_text == "Net Debt to EBITDA, excluding lease liabilities under IFRS 16"
    assert evidence.exceptions == ["excluding lease liabilities under IFRS 16"]
    assert evidence.period_ref == "quarterly"
    assert evidence.produced_by_atom == "read.condition"
    assert evidence.document_version == "v1"


def test_wrong_page_is_blocked_even_if_the_text_exists_elsewhere(monkeypatch):
    response = {"conditions": {"CURRENT_RATIO": [{
        # Реальный текст этой фразы — на странице 6, не 1.
        "page": 1, "quote": "The Borrower shall maintain a Current Ratio of at least 1.2, tested annually.",
        "operator_text": "at least", "threshold_text": "1.2",
    }]}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["CURRENT_RATIO"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert result["CURRENT_RATIO"] == []


def test_paraphrased_quote_is_blocked_not_downgraded(monkeypatch):
    response = {"conditions": {"CURRENT_RATIO": [{
        "page": 6, "quote": "The Borrower must keep working capital healthy.",
        "operator_text": "at least", "threshold_text": "1.2",
    }]}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["CURRENT_RATIO"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert result["CURRENT_RATIO"] == []


def test_incomplete_condition_keeps_the_quote_but_not_a_fabricated_rule(monkeypatch):
    """Клауза реально найдена и процитирована верно, но формулировка оператора
    незнакома парсеру — условие остаётся НЕПОЛНЫМ (`operator=None`), не
    получает угаданное значение."""
    response = {"conditions": {"CURRENT_RATIO": [{
        "page": 6, "quote": "The Borrower shall maintain a Current Ratio of at least 1.2, tested annually.",
        "operator_text": "в соответствии с внутренней политикой", "threshold_text": "1.2",
    }]}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["CURRENT_RATIO"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert len(result["CURRENT_RATIO"]) == 1
    evidence = result["CURRENT_RATIO"][0]
    assert evidence.condition_text  # цитата реальна и подтверждена
    assert evidence.operator is None  # но правило не сформировано


def test_multiple_covenants_for_the_same_metric_are_kept_separate(monkeypatch):
    response = {"conditions": {"CURRENT_RATIO": [
        {
            "page": 6, "quote": "The Borrower shall maintain a Current Ratio of at least 1.2, tested annually.",
            "operator_text": "at least", "threshold_text": "1.2", "subject": "the Borrower",
        },
        {
            "page": 6, "quote": "The Guarantor shall maintain a Current Ratio of at least 1.0, tested annually.",
            "operator_text": "at least", "threshold_text": "1.0", "subject": "the Guarantor",
        },
    ]}}
    _patch_call_json_llm(monkeypatch, response)
    result = read_condition_batch(
        ["CURRENT_RATIO"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
    )
    assert len(result["CURRENT_RATIO"]) == 2
    subjects = {e.subject for e in result["CURRENT_RATIO"]}
    thresholds = {e.threshold_value for e in result["CURRENT_RATIO"]}
    assert subjects == {"the Borrower", "the Guarantor"}
    assert thresholds == {1.2, 1.0}
    assert len({e.evidence_id for e in result["CURRENT_RATIO"]}) == 2


def test_atom_never_records_a_compliance_verdict():
    """Контрактный уровень: `ConditionEvidence` не имеет поля вердикта вовсе
    — атом физически не может «оценить соблюдение условия»."""
    assert "passed" not in ConditionEvidence.model_fields
    assert "compliant" not in ConditionEvidence.model_fields
    assert "verdict" not in ConditionEvidence.model_fields


def test_invalid_model_response_raises_for_the_executor_to_retry(monkeypatch):
    _patch_call_json_llm(monkeypatch, {"not_conditions": {}})
    with pytest.raises(ConditionResponseInvalid):
        read_condition_batch(
            ["CURRENT_RATIO"], document_id="doc-1", document_version="v1", document_text=_DOCUMENT_TEXT,
        )
