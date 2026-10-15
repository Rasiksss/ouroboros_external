"""`assess.judgment` — критерий готовности атома (16b):

1. Все числовые основания суждения ссылаются на существующие проверенные
   доказательства — `supporting_evidence_ids`, не резолвящиеся в `evidence_id`
   контекста, отбрасываются, а не проходят как есть.
2. Модель не изменяет числовые факты и не создаёт новый показатель в тексте
   суждения — вход модели не содержит ничего, кроме уже посчитанных
   value/unit; парсер не подставляет новых чисел.
3. Концепт без фактов в пакете не подаётся модели и не получает суждения
   (недостаток доказательств — явный пробел, не выдуманный вердикт).
4. Модельный сбой не роняет пакет соседних концептов.
"""
from __future__ import annotations

from finengine.contracts import DerivedFact, EvidenceBundleV2, Judgment, ScalarFact
from finengine.skills.atoms.assess_judgment import assess_judgment_batch, assess_judgment_requires
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec


def _bundle() -> EvidenceBundleV2:
    revenue = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", period_ref="2025", value=1500.0, unit="млн. руб.",
    )
    margin = DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0", source_ref="formula:margin",
        concept="EBITDA_MARGIN", period_ref="2025", formula_id="EBITDA_MARGIN", formula_version="1.0.0",
        inputs=["ev-a"], value=0.24,
    )
    return EvidenceBundleV2(run_id="run-1", scalar_facts=[revenue], derived_facts=[margin])


def test_requires_none_without_run_passport_or_concept():
    assert assess_judgment_requires(NodeArgs(concept="REVENUE"), None) == ()


def test_requires_metric_evidence_and_bundle():
    class _FakePassport:
        pass

    result = assess_judgment_requires(NodeArgs(concept="REVENUE"), _FakePassport())
    assert InputSpec(input_type="MetricEvidence", concept="REVENUE") in result
    assert InputSpec(input_type="EvidenceBundleV2") in result


def test_judgment_with_valid_evidence_id_is_published():
    bundle = _bundle()
    revenue_id = bundle.scalar_facts[0].evidence_id

    def fake_call(prompt, system):
        return {"judgments": [{
            "concept": "REVENUE", "verdict": "рост выручки устойчив",
            "supporting_evidence_ids": [revenue_id],
        }]}

    result = assess_judgment_batch(
        ["REVENUE"], bundle=bundle, methodology_text="методика", llm_json_call=fake_call,
    )
    assert len(result["REVENUE"]) == 1
    judgment = result["REVENUE"][0]
    assert isinstance(judgment, Judgment)
    assert judgment.supporting_evidence_ids == [revenue_id]


def test_judgment_with_fabricated_evidence_id_is_dropped():
    bundle = _bundle()

    def fake_call(prompt, system):
        return {"judgments": [{
            "concept": "REVENUE", "verdict": "придуманный вывод",
            "supporting_evidence_ids": ["ev-does-not-exist"],
        }]}

    result = assess_judgment_batch(
        ["REVENUE"], bundle=bundle, methodology_text="методика", llm_json_call=fake_call,
    )
    assert result["REVENUE"] == []


def test_concept_without_facts_is_not_asked_and_gets_no_judgment():
    bundle = _bundle()
    calls: list[str] = []

    def fake_call(prompt, system):
        calls.append(prompt)
        return {"judgments": []}

    result = assess_judgment_batch(
        ["REVENUE", "UNKNOWN_METRIC"], bundle=bundle, methodology_text="методика", llm_json_call=fake_call,
    )
    assert result["UNKNOWN_METRIC"] == []
    assert "UNKNOWN_METRIC" not in calls[0]


def test_all_concepts_without_facts_skip_the_model_call_entirely():
    bundle = EvidenceBundleV2(run_id="run-1")
    called = False

    def fake_call(prompt, system):
        nonlocal called
        called = True
        return {}

    result = assess_judgment_batch(["REVENUE"], bundle=bundle, methodology_text="", llm_json_call=fake_call)
    assert result == {"REVENUE": []}
    assert called is False


def test_model_failure_does_not_raise_and_returns_empty():
    bundle = _bundle()

    def failing_call(prompt, system):
        raise RuntimeError("gateway timeout")

    result = assess_judgment_batch(
        ["REVENUE"], bundle=bundle, methodology_text="методика", llm_json_call=failing_call,
    )
    assert result == {"REVENUE": []}


def test_one_physical_call_covers_the_whole_batch():
    bundle = _bundle()
    calls: list[dict] = []

    def fake_call(prompt, system):
        calls.append({"prompt": prompt})
        return {"judgments": []}

    assess_judgment_batch(["REVENUE", "EBITDA_MARGIN"], bundle=bundle, methodology_text="", llm_json_call=fake_call)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Форма ответа настоящей модели. Реальный прогон на dev (24.09.2026) три раза
# подряд вернул `supporting_evidence_ids` НЕ списком: числом, JSON-строкой со
# списком и обрывком сломанного JSON. Число валило узел `TypeError`, а весь
# граф — отказом. Парсер — «вторая линия защиты», не полагающаяся на
# дисциплину модели: форму поля он чинит, а существование `evidence_id`
# по-прежнему проверяет — выдуманная опора не проходит ни в одном варианте.
def _judge_with(supporting, bundle=None, *, concept="REVENUE"):
    bundle = bundle or _bundle()

    def fake_call(prompt, system):
        return {"judgments": [{"concept": concept, "verdict": "вывод", "supporting_evidence_ids": supporting}]}

    return assess_judgment_batch([concept], bundle=bundle, methodology_text="методика", llm_json_call=fake_call)


def test_supporting_ids_given_as_a_json_string_of_a_list_are_recovered():
    bundle = _bundle()
    revenue_id = bundle.scalar_facts[0].evidence_id

    result = _judge_with(f'["{revenue_id}", "ev-does-not-exist"]', bundle)

    assert [j.supporting_evidence_ids for j in result["REVENUE"]] == [[revenue_id]]


def test_supporting_ids_given_as_one_bare_id_string_are_recovered():
    bundle = _bundle()
    revenue_id = bundle.scalar_facts[0].evidence_id

    result = _judge_with(revenue_id, bundle)

    assert [j.supporting_evidence_ids for j in result["REVENUE"]] == [[revenue_id]]


def test_a_fabricated_id_hidden_in_a_string_is_still_dropped():
    assert _judge_with('["ev-does-not-exist"]') == {"REVENUE": []}


def test_supporting_ids_of_an_unusable_type_drop_the_judgment_instead_of_crashing():
    for unusable in (3, 2.5, None, {"ev": 1}, True):
        assert _judge_with(unusable) == {"REVENUE": []}, unusable


def test_a_broken_json_string_of_ids_drops_the_judgment_instead_of_crashing():
    assert _judge_with('["ev-a", "ev-b') == {"REVENUE": []}


def test_a_top_level_shape_other_than_the_expected_object_gives_no_judgments():
    bundle = _bundle()
    for raw in ({"judgments": 5}, {"judgments": {"concept": "REVENUE"}}, ["not", "an", "object"], "text"):
        result = assess_judgment_batch(
            ["REVENUE"], bundle=bundle, methodology_text="методика", llm_json_call=lambda p, s, raw=raw: raw,
        )
        assert result == {"REVENUE": []}, raw
