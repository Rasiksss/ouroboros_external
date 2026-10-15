"""C0.1: `tests/golden/intent/` — не менее 100 запросов, покрывающих каждый
из 13 значений `IntentKind` (retrofit v9, каталог `docs/build-plan/
26-arhitekturnaya-fiksaciya-v9.md`, §1 — двенадцать значений более ранней
редакции плюс `monitor_changes`) плюс два свойства запроса (`ambiguous`/
`unknown_concept`).

Проверяет форму эталонного набора, а не поведение `intent.parse`. Если этот
тест зелёный, набор готов быть входом для C6 (`test_golden_intent_accuracy.py`).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

PATH = Path(__file__).resolve().parent / "golden" / "intent" / "queries.json"
EXPECTED_CATEGORIES = {
    "full_analysis",
    "extract_registered_metric",
    "extract_ad_hoc_metric",
    "calculate_registered_metric",
    "calculate_ad_hoc_metric",
    "extract_structure",
    "document_qa",
    "compare_metrics",
    "explain_change",
    "validate_report",
    "plan_fact_analysis",
    "monitor_changes",
    "form",
    "ambiguous",
    "unknown_concept",
}
REQUIRED_FIELDS = {"id", "query", "language", "expected_category"}


def _load() -> list[dict]:
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_at_least_one_hundred_queries():
    assert len(_load()) >= 150, "retrofit v9 (Срез 4 DoD): golden set обязан покрывать все 13 GoalType, порог ≥150"


def test_every_query_has_the_required_fields_and_a_known_category():
    for row in _load():
        missing = REQUIRED_FIELDS - row.keys()
        assert not missing, f"{row.get('id')}: отсутствуют поля {missing}"
        assert row["expected_category"] in EXPECTED_CATEGORIES, (
            f"{row['id']}: неизвестная категория {row['expected_category']!r}"
        )
        assert row["language"] in {"ru", "en"}, f"{row['id']}: неизвестный язык {row['language']!r}"


def test_every_category_from_c0_1_is_covered():
    counts = Counter(row["expected_category"] for row in _load())
    missing = EXPECTED_CATEGORIES - set(counts)
    assert not missing, f"нет ни одного примера для категорий: {missing}"
    assert all(n >= 5 for n in counts.values()), f"слишком мало примеров в какой-то категории: {counts}"


def test_ids_are_unique():
    ids = [row["id"] for row in _load()]
    duplicates = [i for i, n in Counter(ids).items() if n > 1]
    assert not duplicates, f"повторяющиеся id: {duplicates}"
