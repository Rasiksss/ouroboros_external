"""`read.value` — критерий готовности атома (docs/build-plan/
15a-srez-05a-locate-read-value-yadro.md, карточка `read.value`):

1. Не менее 97% размеченных фактов совпадают по числу/знаку/периоду/единице/
   базису (замер на golden-наборе — вне этой сессии; здесь — точные примеры
   того же свойства). Числовой разбор (десятичная запятая, разделители тысяч,
   скобки, дефис) не переизобретается — используется существующий
   `bulk_extractor.period_values._coerce_period_values`, уже покрытый своим
   набором тестов; здесь проверяется ТОЛЬКО правильность интеграции
   `CandidateLocator → ScalarFact`.
2. В 100% кейсов отсутствующего показателя не создаётся числовое значение.
3. Каждый успешный факт содержит ссылку на документ/адрес, из которого он
   прочитан (`source_ref` → `CandidateLocator.locator_id`).
4. Частичная ошибка в пакете из нескольких концептов не изменяет статусы
   успешно прочитанных фактов.
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import CandidateLocator, ScalarFact, SourceLocator
from finengine.skills import bulk_extractor as bulk_pkg
from finengine.skills import two_stage_extractor
from finengine.skills.atoms import read_value as read_value_module
from finengine.skills.atoms.read_value import Stage2ResponseInvalid, read_value_batch

_METRICS = [
    {"enum": "REVENUE", "name_en": "Revenue", "name_ru": "Выручка"},
    {"enum": "COST_OF_SALES", "name_en": "Cost of sales", "name_ru": "Себестоимость"},
]


def _candidate(enum: str) -> CandidateLocator:
    return CandidateLocator(
        produced_by_atom="locate", produced_by_atom_version="1.0.0",
        document_id="doc-1", document_version="v1", concept=enum,
        locator=SourceLocator(page=1, table_or_section="T001", text_fragment=f"| {enum} | 1 234,5 |"),
        confidence=0.6, origin="model",
    )


def _patch_call_json_llm(monkeypatch, response: dict):
    """`read_value_batch` валидирует и разбирает СЫРУЮ строку ответа
    (`_recover_stage2_json_object`), не второй (уже распарсенный) элемент
    кортежа `_call_json_llm` — фейк обязан отдавать `raw`, реально
    сериализованный из того же `response`, иначе валидация увидит пустой
    объект и ложно отклонит любой ответ."""
    async def _fake(**kwargs):  # noqa: ARG001
        return response, {}, json.dumps(response)
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)


def test_concept_without_a_single_candidate_is_not_asked(monkeypatch):
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {}, {}, "{}"
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = read_value_batch(
        ["REVENUE"], {"REVENUE": []}, metrics=_METRICS, document_version="v1",
        period_current="2024", period_prev="2023",
    )
    assert result == {"REVENUE": []}
    assert calls == [], "показатель без единого кандидата locate не должен уходить к модели"


def test_one_physical_call_for_the_whole_batch(monkeypatch):
    calls = []

    response = {
        "REVENUE": {"values": {"2024": "1 234,5"}, "unit_text": "RUB"},
        "COST_OF_SALES": {"values": {"2024": "500,0"}, "unit_text": "RUB"},
    }

    async def _fake(**kwargs):
        calls.append(kwargs)
        return response, {}, json.dumps(response)
    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)

    result = read_value_batch(
        ["REVENUE", "COST_OF_SALES"],
        {"REVENUE": [_candidate("REVENUE")], "COST_OF_SALES": [_candidate("COST_OF_SALES")]},
        metrics=_METRICS, document_version="v1", period_current="2024", period_prev="2023",
    )
    assert len(calls) == 1, "read.value обязан спросить модель ОДИН раз на весь батч концептов"
    assert set(result) == {"REVENUE", "COST_OF_SALES"}


def test_ad_hoc_metrics_are_singleton_shards_while_registry_batching_stays_intact(monkeypatch):
    """Two valid neighbouring balance values must not share one free-form
    model response: in a live run it copied current assets into non-current
    assets even though each concept had a correct, focused locator."""
    monkeypatch.setattr(read_value_module.settings, "extract_batch_size", 10)
    metrics = [
        {"enum": "REVENUE"},
        {"enum": "OTHER_NON_CURRENT", "ad_hoc": True},
        {"enum": "OTHER_CURRENT", "ad_hoc": True},
        {"enum": "COST_OF_SALES"},
        {"enum": "EBITDA"},
    ]

    shards = read_value_module._metric_shards(metrics)

    assert [[row["enum"] for row in shard] for shard in shards] == [
        ["REVENUE"], ["OTHER_NON_CURRENT"], ["OTHER_CURRENT"], ["COST_OF_SALES", "EBITDA"],
    ]


def test_large_logical_batch_is_split_by_the_production_stage2_limit(monkeypatch):
    """Регрессия dev 25.09: форма из 20+ строк не должна просить один огромный JSON."""
    enums = [f"METRIC_{index}" for index in range(5)]
    metrics = [{"enum": enum, "name_en": enum, "name_ru": enum} for enum in enums]
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(read_value_module.settings, "extract_batch_size", 2)
    monkeypatch.setattr(read_value_module.settings, "extract_batch_jobs", 2)
    monkeypatch.setattr(read_value_module.settings, "extract_batch_attempts", 1)
    monkeypatch.setattr(
        two_stage_extractor,
        "_build_main_extraction_prompt",
        lambda **kwargs: "|".join(str(metric["enum"]) for metric in kwargs["target_metrics"]),
    )

    async def _fake(**kwargs):
        batch = tuple(kwargs["prompt"].split("|"))
        calls.append(batch)
        response = {
            enum: {"values": {"2024": str(index + 1)}, "unit_text": "RUB"}
            for index, enum in enumerate(batch)
        }
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    result = read_value_batch(
        enums, {enum: [_candidate(enum)] for enum in enums}, metrics=metrics,
        document_version="v1", period_current="2024", period_prev="2023",
    )

    assert len(calls) == 3
    assert all(len(batch) <= 2 for batch in calls)
    assert set(result) == set(enums)
    assert all(result[enum] for enum in enums)


def test_invalid_stage2_shard_is_retried_without_growing_back_to_the_whole_form(monkeypatch):
    enums = ["METRIC_A", "METRIC_B", "METRIC_C"]
    metrics = [{"enum": enum, "name_en": enum, "name_ru": enum} for enum in enums]
    attempts: dict[frozenset[str], int] = {}

    monkeypatch.setattr(read_value_module.settings, "extract_batch_size", 2)
    monkeypatch.setattr(read_value_module.settings, "extract_batch_jobs", 2)
    monkeypatch.setattr(read_value_module.settings, "extract_batch_attempts", 2)
    monkeypatch.setattr(
        two_stage_extractor,
        "_build_main_extraction_prompt",
        lambda **kwargs: "|".join(str(metric["enum"]) for metric in kwargs["target_metrics"]),
    )

    async def _fake(**kwargs):
        batch = tuple(kwargs["prompt"].split("|"))
        key = frozenset(batch)
        attempts[key] = attempts.get(key, 0) + 1
        returned = batch[:-1] if key == frozenset(enums[:2]) and attempts[key] == 1 else batch
        response = {enum: {"values": {"2024": "1"}, "unit_text": "RUB"} for enum in returned}
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    result = read_value_batch(
        enums, {enum: [_candidate(enum)] for enum in enums}, metrics=metrics,
        document_version="v1", period_current="2024", period_prev="2023",
    )

    assert attempts[frozenset(enums[:2])] == 2
    assert attempts[frozenset(enums[2:])] == 1
    assert all(result[enum] for enum in enums)


def test_successful_fact_carries_the_parsed_value_and_the_locator_reference(monkeypatch):
    candidate = _candidate("REVENUE")
    _patch_call_json_llm(monkeypatch, {"REVENUE": {"values": {"2024": "1 234,5"}, "unit_text": "RUB"}})

    result = read_value_batch(
        ["REVENUE"], {"REVENUE": [candidate]}, metrics=_METRICS, document_version="doc-v1",
        period_current="2024", period_prev="2023",
    )
    facts = result["REVENUE"]
    assert len(facts) >= 1
    fact = facts[0]
    assert isinstance(fact, ScalarFact)
    assert fact.concept == "REVENUE"
    assert fact.value == pytest.approx(1234.5)
    assert fact.unit
    assert fact.period_ref
    assert fact.document_version == "doc-v1"
    assert fact.source_ref == candidate.locator_id
    assert fact.produced_by_atom == "read.value"


def test_missing_value_creates_no_scalar_fact_not_a_zero(monkeypatch):
    _patch_call_json_llm(monkeypatch, {"REVENUE": {"values": {}, "unit_text": "RUB"}})
    result = read_value_batch(
        ["REVENUE"], {"REVENUE": [_candidate("REVENUE")]}, metrics=_METRICS, document_version="v1",
        period_current="2024", period_prev="2023",
    )
    assert result["REVENUE"] == []


def test_empty_ad_hoc_singleton_is_retried_without_inventing_a_value(monkeypatch):
    calls = 0
    responses = [
        {"CUSTOM_ROW": {"values": {}, "unit_text": "RUB"}},
        {"CUSTOM_ROW": {"values": {"2024": "8.201"}, "unit_text": "RUB"}},
    ]
    monkeypatch.setattr(read_value_module.settings, "extract_batch_attempts", 2)

    async def _fake(**kwargs):  # noqa: ARG001
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    result = read_value_batch(
        ["CUSTOM_ROW"], {"CUSTOM_ROW": [_candidate("CUSTOM_ROW")]},
        metrics=[{"enum": "CUSTOM_ROW", "ad_hoc": True}], document_version="v1",
        period_current="2024", period_prev="2023",
    )

    assert calls == 2
    assert result["CUSTOM_ROW"][0].value == pytest.approx(8.201)


def test_empty_ad_hoc_singleton_remains_absent_after_retry_budget(monkeypatch):
    calls = 0
    response = {"CUSTOM_ROW": {"values": {}, "unit_text": "RUB"}}
    monkeypatch.setattr(read_value_module.settings, "extract_batch_attempts", 2)

    async def _fake(**kwargs):  # noqa: ARG001
        nonlocal calls
        calls += 1
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    result = read_value_batch(
        ["CUSTOM_ROW"], {"CUSTOM_ROW": [_candidate("CUSTOM_ROW")]},
        metrics=[{"enum": "CUSTOM_ROW", "ad_hoc": True}], document_version="v1",
        period_current="2024", period_prev="2023",
    )

    assert calls == 2
    assert result["CUSTOM_ROW"] == []


def test_partial_registry_batch_retries_only_missing_concepts_once(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(read_value_module.settings, "extract_batch_attempts", 4)
    monkeypatch.setattr(
        two_stage_extractor,
        "_build_main_extraction_prompt",
        lambda **kwargs: "|".join(str(metric["enum"]) for metric in kwargs["target_metrics"]),
    )

    async def _fake(**kwargs):
        asked = tuple(kwargs["prompt"].split("|"))
        calls.append(asked)
        if len(calls) == 1:
            response = {
                "REVENUE": {"values": {"2024": "100"}, "unit_text": "RUB"},
                "COST_OF_SALES": {"values": {}, "unit_text": "RUB"},
            }
        else:
            response = {"COST_OF_SALES": {"values": {"2024": "60"}, "unit_text": "RUB"}}
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    result = read_value_batch(
        ["REVENUE", "COST_OF_SALES"],
        {"REVENUE": [_candidate("REVENUE")], "COST_OF_SALES": [_candidate("COST_OF_SALES")]},
        metrics=_METRICS, document_version="v1", period_current="2024", period_prev="2023",
    )

    assert calls == [("REVENUE", "COST_OF_SALES"), ("COST_OF_SALES",)]
    assert result["REVENUE"][0].value == 100
    assert result["COST_OF_SALES"][0].value == 60


def test_invalid_model_response_raises_for_the_executor_to_retry(monkeypatch):
    """`_recover_stage2_json_object` отклоняет ответ без ожидаемого enum'а —
    `read.value` поднимает исключение, чтобы `GraphExecutor` переспросил ВЕСЬ
    физический батч через свой retry-бюджет, а не тихо промолчал."""
    _patch_call_json_llm(monkeypatch, {"COST_OF_SALES": {"values": {}, "unit_text": "RUB"}})  # REVENUE отсутствует
    with pytest.raises(Stage2ResponseInvalid):
        read_value_batch(
            ["REVENUE"], {"REVENUE": [_candidate("REVENUE")]}, metrics=_METRICS, document_version="v1",
            period_current="2024", period_prev="2023",
        )


def test_one_concept_crash_does_not_lose_the_others(monkeypatch):
    _patch_call_json_llm(monkeypatch, {
        "REVENUE": {"values": {"2024": "1 234,5"}, "unit_text": "RUB"},
        "COST_OF_SALES": {"values": {"2024": "500,0"}, "unit_text": "RUB"},
    })
    original = bulk_pkg._coerce_period_values

    def flaky(info, *, enum=None, **kwargs):
        if enum == "COST_OF_SALES":
            raise RuntimeError("сбой разбора именно этого концепта")
        return original(info, enum=enum, **kwargs)
    monkeypatch.setattr(bulk_pkg, "_coerce_period_values", flaky)

    result = read_value_batch(
        ["REVENUE", "COST_OF_SALES"],
        {"REVENUE": [_candidate("REVENUE")], "COST_OF_SALES": [_candidate("COST_OF_SALES")]},
        metrics=_METRICS, document_version="v1", period_current="2024", period_prev="2023",
    )
    assert len(result["REVENUE"]) >= 1, "сосед по батчу не должен пострадать из-за чужого сбоя"
    assert result["COST_OF_SALES"] == []


# ---------------------------------------------------------------------------
# Срез 30, real-model smoke 24.09: EXTRACT_AD_HOC_METRIC на реальном документе
# (Магнит FY2024, «средневзвешенное количество акций») давал `read.value` без
# единого вызова модели и узел `succeeded` с нулём фактов. `locate` ставит
# `CandidateLocator.concept` ВЕРХНИМ регистром, `read_value_batch` ключует
# кандидатов верхним регистром (докстринг модуля), а `_read_value_batch_real`
# собирал словарь по исходному регистру фразы пользователя: для ASCII-enum
# реестра это совпадает, для русской фразы — никогда. Дальше `norm.unit`/
# `norm.time`/`verify.bounds` сравнивают `e.concept == args.concept` точно.
# Интеграционные тесты этого не видели: их fake-executors обходят этот glue.
_AD_HOC_TERM = "средневзвешенное количество акций"


def _ad_hoc_ctx():
    from types import SimpleNamespace

    passport = SimpleNamespace(
        classification=SimpleNamespace(
            company_type="non_financial", standard="ifrs", language="ru", profile_id="test-cell",
        ),
    )
    return SimpleNamespace(
        run_passport=passport, run_id="run-1",
        inputs=SimpleNamespace(document_text="| Средневзвешенное количество акций | 67 849 | 88 483 |"),
        dependencies=lambda: (_candidate(_AD_HOC_TERM.upper()),),
    )


def test_ad_hoc_phrase_with_lowercase_letters_is_asked_and_keyed_by_the_requested_concept(monkeypatch):
    from finengine.skills.atoms import graph_atoms
    from finengine.skills.spec.atom_registry import NodeArgs

    calls = []
    response = {_AD_HOC_TERM.upper(): {"values": {"2024": "67 849"}, "unit_text": "тыс."}}

    async def _fake(**kwargs):
        calls.append(kwargs)
        return response, {}, json.dumps(response, ensure_ascii=False)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)
    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": []})

    result = graph_atoms._read_value_batch_real(
        [NodeArgs(concept=_AD_HOC_TERM, period_ref="2024")], _ad_hoc_ctx(),
    )

    assert len(calls) == 1, "модель обязана быть спрошена: у концепта есть кандидат locate"
    assert list(result) == [_AD_HOC_TERM], "узел ждёт результат под ЗАПРОШЕННЫМ концептом"
    assert len(result[_AD_HOC_TERM]) == 1


def test_extracted_metric_wrapper_keeps_the_requested_concept_casing():
    from finengine.skills.atoms import graph_atoms
    from finengine.skills.spec.atom_registry import NodeArgs

    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=_AD_HOC_TERM.upper(), period_ref="FY 2024", value=67.849, unit="млн.",
    )
    wrapped = graph_atoms._wrap_extracted_metric(lambda items, ctx: {_AD_HOC_TERM: (fact,)})

    metric = wrapped([NodeArgs(concept=_AD_HOC_TERM, period_ref="2024", extra={"ad_hoc_spec_id": "s1"})], None)[_AD_HOC_TERM][0]

    assert metric.concept == _AD_HOC_TERM, "norm.unit/norm.time/verify.bounds сверяют concept точно"
    assert metric.definition_origin == "ad_hoc"


def test_ad_hoc_stock_converts_a_duration_shaped_model_period_to_the_balance_date(monkeypatch):
    """Модель читает правильную строку баланса, но общий период документа
    (`6M 2026`) не должен превращать остаток на 30 июня в поток за полугодие."""
    from finengine.contracts import AdHocConceptSpec
    from finengine.skills.atoms import graph_atoms
    from finengine.skills.atoms.ad_hoc_concepts import dump_specs
    from finengine.skills.spec.atom_registry import NodeArgs

    concept = "Прочие внеоборотные активы"
    extra = {
        "ad_hoc_spec_id": "stock-1",
        "ad_hoc_concepts": dump_specs([
            AdHocConceptSpec(term=concept, name_ru=concept, value_kind="stock"),
        ]),
    }
    model_fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=concept.upper(), period_ref="6M 2026", value=424439.405, unit="млн. руб.",
    )

    monkeypatch.setattr(graph_atoms, "registry_for_passport", lambda passport: {"extractable": []})
    monkeypatch.setattr(read_value_module, "read_value_batch", lambda *args, **kwargs: {
        concept.upper(): [model_fact],
    })

    result = graph_atoms._read_value_batch_real(
        [NodeArgs(concept=concept, period_ref="на 30.06.2026", extra=extra)], _ad_hoc_ctx(),
    )

    assert result[concept][0].period_ref == "30.06.2026"


def test_extracted_metric_wrapper_uses_ad_hoc_stock_kind_for_period_filtering():
    from finengine.contracts import AdHocConceptSpec
    from finengine.skills.atoms import graph_atoms
    from finengine.skills.atoms.ad_hoc_concepts import dump_specs
    from finengine.skills.spec.atom_registry import NodeArgs

    concept = "Прочие оборотные активы"
    extra = {
        "ad_hoc_spec_id": "stock-2",
        "ad_hoc_concepts": dump_specs([
            AdHocConceptSpec(term=concept, name_ru=concept, value_kind="stock"),
        ]),
    }
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-2",
        concept=concept.upper(), period_ref="6M 2026", value=431.706, unit="млн. руб.",
    )
    wrapped = graph_atoms._wrap_extracted_metric(lambda items, ctx: {concept: (fact,)})

    metrics = wrapped(
        [NodeArgs(concept=concept, period_ref="на 30.06.2026", extra=extra)], None,
    )[concept]

    assert len(metrics) == 1
    assert metrics[0].period_ref == "6M 2026"


def test_a_point_in_time_metric_survives_the_requested_period_filter_for_a_year():
    """Срез 30, real-model smoke: `read.value` извлёк PPE на 31.12.2024 верно,
    а фильтр запрошенного периода (Срез 29a) сравнивал метку факта
    `31.12.2024` с нормализованным запросом `FY 2024` и отбрасывал баланс —
    любой балансовый показатель при запросе «за 2024 год» был недостижим
    (и как самостоятельное извлечение, и как зависимость расчёта). Вид периода
    берётся у самого показателя (`period_kind_for_enum`); факт ДРУГОГО периода
    по-прежнему отбрасывается."""
    from finengine.skills.atoms import graph_atoms
    from finengine.skills.spec.atom_registry import NodeArgs

    def _fact(period: str, value: float) -> ScalarFact:
        return ScalarFact(
            produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
            concept="PPE", period_ref=period, value=value, unit="млн. руб.",
        )

    wrapped = graph_atoms._wrap_extracted_metric(
        lambda items, ctx: {"PPE": (_fact("31.12.2024", 28782.0), _fact("31.12.2023", 27222.0))},
    )

    kept = wrapped([NodeArgs(concept="PPE", period_ref="2024")], None)["PPE"]

    assert [m.period_ref for m in kept] == ["31.12.2024"]
