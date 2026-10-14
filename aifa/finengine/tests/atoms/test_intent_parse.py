"""`intent.parse` — критерий готовности атома (docs/build-plan/14-srez-04-
graf-istochnik-istiny.md, раздел «Критерий готовности атома»). Каждый пункт
ниже — отдельный тест с прямой ссылкой на формулировку карточки.

Детерминированная часть конвейера (валидация схемы, фильтрация по кандидатам,
`root_from_goal`) тестируется здесь БЕЗ обращения к живой модели — через
`complete_fn`, тот же приём подмены единственной модельной границы, каким уже
пользуется `tests/integration/test_graph_*` для `locate`/`read.value`
(`make_fake_locate_and_read_value_executors`). Живая модельная точность на
золотом наборе (пункт 1 критерия) — отдельный тест,
`test_golden_intent_accuracy.py`: ему нужен настоящий модельный шлюз, здесь
он был бы недостоверен.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec, GoalIR, TargetNode
from finengine.skills.atoms.intent import (
    IntentParseError,
    RootTarget,
    candidate_concepts,
    parse_intent,
    root_from_goal,
)
from finengine.skills.atoms import intent as intent_module


def _fake_complete(responses: list[str]):
    """`complete_fn`-подмена: возвращает `responses` по очереди по одному
    вызову, считает число вызовов (`.calls`) — тесты retry-политики проверяют
    именно его, а не только итоговый результат."""
    calls: list[str] = []

    async def _complete(system: str, prompt: str, *, model_version: str | None = None) -> str:
        calls.append(prompt)
        return responses[len(calls) - 1]

    _complete.calls = calls  # type: ignore[attr-defined]
    return _complete


def _ok_response(**overrides) -> str:
    base = {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": "Северсталь", "period_ref": "2024", "role": None,
        "unresolved": [], "ambiguities": [],
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_quick_ratio_method_is_consistent_in_main_and_focused_prompts():
    for prompt in (intent_module._SYSTEM_PROMPT, intent_module._COMPOUND_CLAUSE_REPAIR_SYSTEM):
        assert "Итого оборотных активов - Запасы" in prompt
        assert "Итого краткосрочных обязательств" in prompt


def test_comparison_with_a_transformation_or_external_series_stays_one_comparison_goal():
    prompt = intent_module._SYSTEM_PROMPT

    assert "нормировать или привести к базе 100" in prompt
    assert "с внешним рядом или URL" in prompt
    assert "не понижай цель до extract_registered_metric" in prompt


def test_intent_model_call_uses_the_bounded_router_profile(monkeypatch):
    seen = {}

    async def _complete(prompt, **kwargs):
        seen.update(kwargs)
        return "{}"

    from finengine.llm import openrouter

    monkeypatch.setattr(openrouter, "complete", _complete)

    asyncio.run(intent_module._default_complete("system", "prompt", model_version=None))

    assert seen["retries"] == 1
    assert seen["max_output"] == 6000
    assert seen["reasoning_effort"] == "low"


# ---------------------------------------------------------------------------
# Пункт 2: «Неизвестный концепт и неоднозначное имя в 100% соответствующих
# тестов попадают в unresolved/ambiguities, а не превращаются в выдуманный
# код показателя.»
async def test_unknown_concept_from_model_is_filtered_into_unresolved_not_trusted():
    # Кандидаты для этого запроса не содержат FAKE_HALLUCINATED_ENUM — модель
    # его всё равно вернула (симулируем сбой промпт-дисциплины), и
    # детерминированная проверка ПОСЛЕ ответа обязана его отсечь.
    fake = _fake_complete([_ok_response(targets=[{"concept": "FAKE_HALLUCINATED_ENUM"}])])
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert goal.targets == []
    assert "FAKE_HALLUCINATED_ENUM" in goal.unresolved


async def test_ambiguous_term_is_not_guessed():
    resp = _ok_response(
        intent_kind="extract_registered_metric", targets=[],
        ambiguities=[{"term": "прибыль", "candidates": ["NET_INCOME", "EBITDA_MARGIN"], "reason": "не указан вид прибыли"}],
    )
    fake = _fake_complete([resp])
    goal = await parse_intent("покажи прибыль Магнита за 2024", complete_fn=fake)
    assert goal.targets == []
    assert len(goal.ambiguities) == 1
    assert goal.ambiguities[0].term == "прибыль"
    assert set(goal.ambiguities[0].candidates) == {"NET_INCOME", "EBITDA_MARGIN"}


async def test_genuinely_unresolved_concept_reaches_unresolved_verbatim():
    resp = _ok_response(intent_kind="extract_registered_metric", targets=[], unresolved=["мультипликатор счастья инвестора"])
    fake = _fake_complete([resp])
    goal = await parse_intent("дай мультипликатор счастья инвестора для Яндекса", complete_fn=fake)
    assert goal.unresolved == ["мультипликатор счастья инвестора"]
    assert goal.targets == []


# ---------------------------------------------------------------------------
# Пункт 3: «Запрос полного анализа создаёт один GoalIR для полного результата
# и не содержит придуманного моделью списка показателей.»
async def test_full_analysis_never_carries_a_metric_list_even_if_model_tries():
    # Модель нарушила инструкцию и всё равно прислала список — детерминированная
    # проверка обязана его отбросить независимо от промпта (A1.5).
    resp = _ok_response(
        intent_kind="full_analysis",
        targets=[{"concept": "SALES_REVENUE"}, {"concept": "NET_INCOME"}],
        entity_ref="Северсталь", period_ref="2024",
    )
    fake = _fake_complete([resp])
    goal = await parse_intent("сделай полный анализ Северстали за 2024", complete_fn=fake)
    assert goal.intent_kind == "full_analysis"
    assert goal.targets == []

    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "ReportOutput"
    assert roots[0].root.concept_id is None
    assert roots[0].root.entity_ref == "Северсталь"


async def test_explain_change_carries_validated_analysis_concepts_and_all_period_scope():
    """EXPLAIN_CHANGE owns a semantic concept list; it must not smuggle the
    list through ``targets`` (reserved for scalar extraction/calculation).
    Срез 31: пустой ``period_ref`` = «все доступные периоды» (отдельного
    ``period_scope`` больше нет)."""
    resp = _ok_response(
        intent_kind="explain_change", targets=[], period_ref=None,
        analysis_concepts=[
            "LONG_TERM_FINANCIAL_INVESTMENTS",
            "SHORT_TERM_FINANCIAL_INVESTMENTS",
        ],
    )
    fake = _fake_complete([resp])

    goal = await parse_intent(
        "Объясни разницу финансовых вложений между всеми периодами",
        complete_fn=fake,
    )

    assert goal.targets == []
    assert goal.parameters["analysis_concepts"] == [
        "LONG_TERM_FINANCIAL_INVESTMENTS",
        "SHORT_TERM_FINANCIAL_INVESTMENTS",
    ]
    assert goal.period_ref is None, "период не назван — берутся все доступные"
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "NarrativeOutput"
    assert roots[0].root.period_ref is None
    assert roots[0].root_extra["requested_concepts"] == (
        "LONG_TERM_FINANCIAL_INVESTMENTS,SHORT_TERM_FINANCIAL_INVESTMENTS"
    )


async def test_explain_change_rejects_analysis_concept_outside_candidates():
    resp = _ok_response(
        intent_kind="explain_change", targets=[], period_ref=None,
        analysis_concepts=["MADE_UP_CONCEPT"],
    )
    fake = _fake_complete([resp])

    goal = await parse_intent(
        "Объясни разницу финансовых вложений между всеми периодами",
        complete_fn=fake,
    )

    assert goal.parameters.get("analysis_concepts") is None
    assert "MADE_UP_CONCEPT" in goal.unresolved
    assert root_from_goal(goal) == []


# ---------------------------------------------------------------------------
# Пункт 4: «Выход проходит схему в 100% успешных случаев; после одной
# разрешённой повторной попытки невалидный ответ модели переводится в явную
# ошибку, а не чинится скрытой эвристикой.»
async def test_valid_response_passes_schema_first_try():
    fake = _fake_complete([_ok_response()])
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert isinstance(goal, GoalIR)
    assert len(fake.calls) == 1


async def test_invalid_json_retried_once_then_succeeds():
    fake = _fake_complete(["не json вообще", _ok_response()])
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert isinstance(goal, GoalIR)
    assert len(fake.calls) == 2
    assert "Предыдущий ответ был невалиден" in fake.calls[1]


async def test_invalid_json_twice_raises_explicit_error_not_silently_fixed():
    fake = _fake_complete(["не json", "тоже не json"])
    with pytest.raises(IntentParseError):
        await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert len(fake.calls) == 2  # ровно одна повторная попытка, не бесконечный цикл


async def test_truncated_explicit_compound_is_reparsed_as_bounded_focused_clauses():
    query = """По отчётности одним составным планом:
1. Ответь на вопрос о документе A.
2. Ответь на вопрос о документе B.
3. Ответь на вопрос о документе C.
4. Ответь на вопрос о документе D.

Для каждого результата покажи источник."""

    def clause(index: int) -> str:
        return json.dumps({
            "goal_ref": f"draft-{index}", "depends_on": [],
            "intent_kind": "document_qa", "targets": [], "analysis_concepts": [],
            "ad_hoc_concept": None, "ad_hoc_formula": None,
            "entity_ref": None, "period_ref": None, "role": None,
            "unresolved": [], "ambiguities": [], "correction": None,
        }, ensure_ascii=False)

    fake = _fake_complete(['{"route":"plan","intent_kind":"document_qa"', *(clause(i) for i in range(1, 5))])

    goal = await parse_intent(query, complete_fn=fake)

    assert [item.goal_ref for item in goal.clauses()] == ["goal-1", "goal-2", "goal-3", "goal-4"]
    assert [item.intent_kind for item in goal.clauses()] == ["document_qa"] * 4
    assert len(fake.calls) == 5, "один оборванный конверт + по одному ограниченному вызову на цель"


async def test_focused_numbered_clause_retries_its_own_invalid_formula():
    query = """Одним планом:
1. Рассчитай показатель A.
2. Извлеки выручку."""
    invalid_formula = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula={"name_ru": "A", "expression": "", "components": {}},
    )
    valid_registered = _ok_response(period_ref=None)
    valid_formula = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula={"name_ru": "A", "expression": "2 + 2", "components": {}},
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"calculate_ad_hoc_metric"',
        invalid_formula, valid_formula, valid_registered,
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert goal.parameters["ad_hoc_formula"]["expression"] == "2 + 2"
    assert len(goal.clauses()) == 2
    assert "Предыдущий ответ этой цели не прошёл схему" in fake.calls[2]


async def test_focused_numbered_clause_has_a_third_local_formula_attempt():
    query = """Одним планом:
1. Рассчитай стандартный коэффициент A, формулу подбери сам.
2. Извлеки выручку."""
    invalid_formula = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula={"name_ru": "A", "expression": "", "components": {}},
    )
    valid_formula = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula={"name_ru": "A", "expression": "2 + 2", "components": {}},
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"calculate_ad_hoc_metric"',
        invalid_formula, invalid_formula, valid_formula, _ok_response(period_ref=None),
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert goal.parameters["ad_hoc_formula"]["expression"] == "2 + 2"
    assert len(fake.calls) == 5


async def test_focused_numbered_clause_normalizes_string_targets_without_retry():
    query = """Одним планом:
1. Извлеки выручку.
2. Извлеки прибыль."""
    string_target = _ok_response(targets=["SALES_REVENUE"], period_ref=None)
    valid_profit = _ok_response(targets=[{"concept": "NET_INCOME"}], period_ref=None)
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_registered_metric"',
        string_target, valid_profit,
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert [target.concept for target in goal.targets] == ["SALES_REVENUE"]
    assert [target.concept for target in goal.additional_goals[0].targets] == ["NET_INCOME"]
    assert len(fake.calls) == 3


async def test_generic_paid_dividends_do_not_narrow_to_noncontrolling_interests():
    query = """Одним планом:
1. Извлеки уплаченные дивиденды.
2. Извлеки выручку."""
    narrow = _ok_response(
        targets=["DIVIDENDS_PAID_NONCONTROLLING_INTERESTS"], period_ref=None,
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_registered_metric"',
        narrow, _ok_response(period_ref=None),
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert [target.concept for target in goal.targets] == ["DIVIDENDS"]
    assert len(fake.calls) == 3


async def test_focused_numbered_registered_clause_retries_empty_targets():
    query = """Одним планом:
1. Рассчитай коммерческие и управленческие расходы.
2. Извлеки выручку."""
    empty_registered = _ok_response(
        intent_kind="calculate_registered_metric", targets=[], period_ref=None,
    )
    valid_expenses = _ok_response(
        intent_kind="calculate_registered_metric",
        targets=[{"concept": "OPERATING_EXPENSES_TOTAL"}], period_ref=None,
    )
    valid_revenue = _ok_response(period_ref=None)
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"calculate_registered_metric"',
        empty_registered, valid_expenses, valid_revenue,
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert [target.concept for target in goal.targets] == ["OPERATING_EXPENSES_TOTAL"]
    assert "targets пуст" in fake.calls[2]


async def test_focused_numbered_ad_hoc_clause_retries_value_kind_other():
    query = """Одним планом:
1. Извлеки балансовую стоимость товарного знака.
2. Извлеки выручку."""

    def ad_hoc(kind: str) -> str:
        return _ok_response(
            intent_kind="extract_ad_hoc_metric", targets=[], period_ref=None,
            ad_hoc_concept={
                "name_ru": "Балансовая стоимость товарного знака",
                "name_en": "Trademark carrying amount",
                "aliases_ru": ["Средства индивидуализации"], "aliases_en": ["Trademark"],
                "definition": "Балансовая стоимость товарного знака на отчётную дату.",
                "hint": "Расшифровка нематериальных активов.", "value_kind": kind,
            },
        )

    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_ad_hoc_metric"',
        ad_hoc("other"), ad_hoc("stock"), _ok_response(period_ref=None),
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert goal.parameters["ad_hoc_concept"]["value_kind"] == "stock"
    assert "value_kind должен быть stock|flow|ratio" in fake.calls[2]


async def test_focused_numbered_clause_normalizes_string_ad_hoc_concept():
    query = """Одним планом:
1. Извлеки балансовую стоимость товарного знака на 30.06.2026.
2. Извлеки выручку."""
    string_spec = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept="Балансовая стоимость товарного знака",
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_ad_hoc_metric"',
        string_spec, _ok_response(period_ref=None),
    ])

    goal = await parse_intent(query, complete_fn=fake)

    spec = goal.parameters["ad_hoc_concept"]
    assert spec["name_ru"] == "Балансовая стоимость товарного знака"
    assert spec["value_kind"] == "stock"
    assert len(fake.calls) == 3


async def test_focused_numbered_clause_backfills_missing_explicit_period_from_user_text():
    query = """По отчётности на 30.06.2026 одним планом:
1. Извлеки выручку.
2. Извлеки чистую прибыль."""
    missing_period = _ok_response(period_ref=None)
    profit = _ok_response(
        targets=[{"concept": "NET_INCOME"}], period_ref=None,
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_registered_metric"',
        missing_period, profit,
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert goal.period_ref == "30.06.2026"
    assert goal.additional_goals[0].period_ref == "30.06.2026"
    assert len(fake.calls) == 3, "период из текста не требует ещё одного LLM-вызова"


async def test_focused_numbered_ad_hoc_clause_retries_query_copied_as_its_name():
    query = """Одним планом:
1. Извлеки прочие внеоборотные активы.
2. Извлеки выручку."""
    copied_query = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_concept={
            "name_ru": "Очень длинная копия всего запроса " * 8,
            "name_en": "Copied query", "aliases_ru": [], "aliases_en": [],
            "definition": "Копия запроса.", "hint": "отчётность", "value_kind": "stock",
        },
    )
    repaired = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_concept={
            "name_ru": "Прочие внеоборотные активы", "name_en": "Other non-current assets",
            "aliases_ru": [], "aliases_en": [], "definition": "Остаток на дату.",
            "hint": "баланс", "value_kind": "stock",
        },
    )
    fake = _fake_complete([
        '{"route":"plan","intent_kind":"extract_ad_hoc_metric"',
        copied_query, repaired, _ok_response(period_ref=None),
    ])

    goal = await parse_intent(query, complete_fn=fake)

    assert goal.parameters["ad_hoc_concept"]["name_ru"] == "Прочие внеоборотные активы"
    assert "копирует весь запрос" in fake.calls[2]


async def test_schema_valid_empty_ad_hoc_placeholder_repairs_the_complete_goal_set():
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept={"term": "полный запрос", "name_ru": "", "value_kind": "other"},
        additional_goals=[],
    )

    def ad_hoc(name: str, kind: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": "Лукойл", "period_ref": "30.06.2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [name], "aliases_en": [],
                "definition": name, "hint": "отчётность", "value_kind": kind,
            },
        }

    repaired = json.dumps({"goals": [
        ad_hoc("Прочие внеоборотные активы", "stock"),
        ad_hoc("Прочие доходы", "flow"),
    ]}, ensure_ascii=False)
    still_underspecified = json.dumps({"goals": [
        ad_hoc("Прочие внеоборотные активы", "other"),
        ad_hoc("Прочие доходы", "other"),
    ]}, ensure_ascii=False)
    fake = _fake_complete([placeholder, still_underspecified, repaired])

    goal = await parse_intent("полный запрос", complete_fn=fake)

    assert len(goal.clauses()) == 2
    assert len(fake.calls) == 3
    assert goal.parameters["ad_hoc_concept"]["value_kind"] == "stock"
    assert goal.additional_goals[0].parameters["ad_hoc_concept"]["value_kind"] == "flow"


async def test_null_ad_hoc_concept_repairs_instead_of_using_the_whole_query_as_name():
    query = (
        "Извлеки прочие внеооборотные и оборотные активы, прочие доходы и расходы "
        "за полугодие 2026. Для каждой цифры укажи единицу, период и точный источник в отчёте. "
        "Не подменяй эти статьи похожими метриками из реестра."
    )
    assert len(" ".join(query.split())) > 160
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept=None, additional_goals=[],
    )

    def ad_hoc(name: str, kind: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": None, "period_ref": "30.06.2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [], "aliases_en": [],
                "definition": name, "hint": "отчётность", "value_kind": kind,
            },
        }

    repaired = json.dumps({"goals": [
        ad_hoc("Прочие внеооборотные активы", "stock"),
        ad_hoc("Прочие доходы", "flow"),
    ]}, ensure_ascii=False)
    fake = _fake_complete([placeholder, repaired])

    goal = await parse_intent(query, complete_fn=fake)

    assert [item.parameters["ad_hoc_concept"]["name_ru"] for item in goal.clauses()] == [
        "Прочие внеооборотные активы", "Прочие доходы",
    ]
    assert goal.additional_goals[0].period_ref == "6M 2026"


async def test_ad_hoc_name_that_copies_the_whole_query_triggers_atomic_goal_set_repair():
    query = "Сколько прочих активов и какая сумма прочих доходов за полугодие 2026?"
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="6M 2026",
        ad_hoc_concept={"name_ru": query, "value_kind": "flow"}, additional_goals=[],
    )

    def ad_hoc(name: str, kind: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": None, "period_ref": "6M 2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [name], "aliases_en": [],
                "definition": name, "hint": "отчётность", "value_kind": kind,
            },
        }

    repaired = json.dumps({"goals": [
        ad_hoc("Прочие активы", "stock"), ad_hoc("Прочие доходы", "flow"),
    ]}, ensure_ascii=False)
    fake = _fake_complete([placeholder, repaired])

    goal = await parse_intent(query, complete_fn=fake)

    assert [item.parameters["ad_hoc_concept"]["name_ru"] for item in goal.clauses()] == [
        "Прочие активы", "Прочие доходы",
    ]


async def test_atomic_goal_set_repair_retries_a_goal_that_still_copies_the_query():
    query = "Извлеки пять незарегистрированных статей из отчёта"
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept={"name_ru": query, "value_kind": "stock"}, additional_goals=[],
    )

    def goal(name: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": None, "period_ref": "30.06.2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [], "aliases_en": [],
                "definition": name, "hint": "отчётность", "value_kind": "stock",
            },
        }

    copied = json.dumps({"goals": [goal(query * 4)]}, ensure_ascii=False)
    repaired = json.dumps({"goals": [goal("Прочие внеооборотные активы")]}, ensure_ascii=False)
    fake = _fake_complete([placeholder, copied, repaired])

    parsed = await parse_intent(query, complete_fn=fake)

    assert parsed.parameters["ad_hoc_concept"]["name_ru"] == "Прочие внеооборотные активы"
    assert "копирует весь запрос" in fake.calls[2]


async def test_atomic_goal_set_uses_explicit_half_year_for_flow_articles():
    query = "Извлеки прочие активы на 30.06.2026 и прочие доходы за полугодие"
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept={"name_ru": query, "value_kind": "stock"}, additional_goals=[],
    )

    def goal(name: str, kind: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": None, "period_ref": "30.06.2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [], "aliases_en": [],
                "definition": name, "hint": "отчётность", "value_kind": kind,
            },
        }

    repaired = json.dumps({"goals": [
        goal("Прочие активы", "stock"), goal("Прочие доходы", "flow"),
    ]}, ensure_ascii=False)

    parsed = await parse_intent(query, complete_fn=_fake_complete([placeholder, repaired]))

    assert parsed.period_ref == "30.06.2026"
    assert parsed.additional_goals[0].period_ref == "6M 2026"


async def test_atomic_goal_set_normalizes_null_list_fields_for_ad_hoc_goals():
    query = "Длинный составной запрос о пяти незарегистрированных статьях " * 3
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], ad_hoc_concept=None,
        additional_goals=[],
    )
    repaired = json.dumps({"goals": [{
        "intent_kind": "extract_ad_hoc_metric", "targets": None, "analysis_concepts": None,
        "entity_ref": None, "period_ref": "30.06.2026", "role": None,
        "unresolved": None, "ambiguities": None, "depends_on": None,
        "ad_hoc_concept": {
            "name_ru": "Прочие внеооборотные активы", "name_en": "Other non-current assets",
            "aliases_ru": [], "aliases_en": [], "definition": "Остаток на дату.",
            "hint": "баланс", "value_kind": "stock",
        },
    }]}, ensure_ascii=False)

    parsed = await parse_intent(query, complete_fn=_fake_complete([placeholder, repaired]))

    assert parsed.parameters["ad_hoc_concept"]["name_ru"] == "Прочие внеооборотные активы"
    assert parsed.targets == [] and parsed.unresolved == [] and parsed.depends_on == []


async def test_atomic_goal_set_repairs_one_malformed_concept_in_isolation():
    query = "Длинный составной запрос о пяти незарегистрированных статьях " * 3
    placeholder = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], ad_hoc_concept=None,
        additional_goals=[],
    )
    malformed_set = json.dumps({"goals": [{
        "intent_kind": "extract_ad_hoc_metric", "targets": None, "analysis_concepts": None,
        "entity_ref": None, "period_ref": "30.06.2026", "role": None,
        "unresolved": None, "ambiguities": None, "depends_on": None,
        "ad_hoc_concept": "Прочие внеооборотные активы",
    }]}, ensure_ascii=False)
    repaired_goal = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], period_ref="30.06.2026",
        ad_hoc_concept={
            "name_ru": "Прочие внеооборотные активы", "name_en": "Other non-current assets",
            "aliases_ru": [], "aliases_en": [], "definition": "Остаток на дату.",
            "hint": "баланс", "value_kind": "stock",
        },
    )
    fake = _fake_complete([placeholder, malformed_set, repaired_goal])

    parsed = await parse_intent(query, complete_fn=fake)

    assert len(fake.calls) == 3
    assert parsed.parameters["ad_hoc_concept"]["name_ru"] == "Прочие внеооборотные активы"
    assert "Исправь только цель 1" in fake.calls[2]


async def test_unknown_intent_kind_counts_as_invalid_schema():
    fake = _fake_complete([_ok_response(intent_kind="something_else"), _ok_response()])
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert goal.intent_kind == "extract_registered_metric"
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Пункт 5: «Атом не выполняет извлечение, расчёты и не задаёт порядок
# выполнения атомов.» — GoalIR не несёт ничего похожего на факты/план; сам
# атом не трогает ни один граф/исполнитель за пределами `complete_fn`.
async def test_atom_does_not_touch_extraction_or_planning_modules():
    fake = _fake_complete([_ok_response()])
    goal = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake)
    assert not hasattr(goal, "scalar_facts")
    assert not hasattr(goal, "nodes")
    assert goal.parameters == {}  # атом не кладёт туда побочных «шагов плана»


# ---------------------------------------------------------------------------
# Минимальный набор сценариев карточки: «Разные формулировки одного
# показателя дают одну цель.»
async def test_different_phrasings_of_the_same_metric_resolve_to_the_same_target():
    fake_ru = _fake_complete([_ok_response(entity_ref="Северсталь")])
    goal_ru = await parse_intent("какая выручка у Северстали за 2024 год", complete_fn=fake_ru)
    fake_en = _fake_complete([_ok_response(entity_ref="Severstal")])
    goal_en = await parse_intent("what was Severstal's revenue in 2024", complete_fn=fake_en)
    assert [t.concept for t in goal_ru.targets] == [t.concept for t in goal_en.targets] == ["SALES_REVENUE"]


# ---------------------------------------------------------------------------
# Пункт из краевых случаев Среза 4 (§38 в 99-matrica.md): невалидная роль не
# проходит как есть.
async def test_invalid_role_is_dropped_not_trusted():
    resp = _ok_response(intent_kind="form", role="investment_banking")
    fake = _fake_complete([resp])
    goal = await parse_intent("собери форму по Северстали", complete_fn=fake)
    assert goal.role is None
    assert "role:investment_banking" in goal.unresolved


async def test_form_without_named_role_becomes_an_ambiguity():
    resp = _ok_response(intent_kind="form", role=None)
    fake = _fake_complete([resp])
    goal = await parse_intent("собери форму по Северстали", complete_fn=fake)
    assert goal.ambiguities and goal.ambiguities[0].term.startswith("форма")


# ---------------------------------------------------------------------------
# `candidate_concepts`: ограниченный словарь, не весь реестр.
def test_candidate_concepts_finds_known_term():
    cands = candidate_concepts("какая выручка у Северстали за 2024 год")
    assert "SALES_REVENUE" in cands


def test_candidate_concepts_is_not_the_whole_registry():
    from finengine.skills.metric_schema import registry_for_profile

    full_size = len(registry_for_profile("ifrs_nf_ru")["extractable"])
    cands = candidate_concepts("какая выручка у Северстали за 2024 год")
    assert len(cands) < full_size


def test_compound_candidates_keep_exact_multiword_metrics_before_broad_matches():
    cands = candidate_concepts(
        "Рассчитай коммерческие и управленческие расходы; "
        "проценты к уплате раздели на выручку. " + "показатель отчётности " * 30,
    )
    assert "OPERATING_EXPENSES_TOTAL" in cands
    assert "INTEREST_EXPENSE_ACCRUED" in cands


def test_candidate_concepts_empty_for_out_of_domain_query():
    cands = candidate_concepts("зюзюзюзю бла бла бла")
    assert cands == {}


# ---------------------------------------------------------------------------
# `root_from_goal`: A1.3 — ровно один корень на full_analysis/form, N корней
# для N явно названных показателей точечного запроса (intent-090 золотого
# набора: «два конкретных показателя... всё ещё точечный запрос»).
def test_root_from_goal_extract_registered_metric_multiple_targets():
    goal = GoalIR(
        raw_query="q", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar"),
                 TargetNode(concept="NET_INCOME", output_kind="scalar")],
        entity_ref="ent", period_ref="2024",
    )
    roots = root_from_goal(goal)
    assert len(roots) == 2
    assert all(isinstance(r, RootTarget) for r in roots)
    assert all(r.root.output_type == "ExtractedMetric" for r in roots)
    assert {r.root.concept_id for r in roots} == {"SALES_REVENUE", "NET_INCOME"}
    assert all(r.root.entity_ref == "ent" and r.root.period_ref == "2024" for r in roots)


# ---------------------------------------------------------------------------
# Аудит Среза 4 (18.09, 00-kak-ispolzovat.md): enum goal_type расширен с 3
# иллюстративных значений до каталога из 11 (docs/build-plan/
# 26-prilozhenie-ad-hoc-i-goalir-katalog.md) + `form`. Ниже — минимальные
# регрессионные тесты на НОВОЕ ветвление (widened условия), без изменения
# самой проверки концептов/unresolved/ambiguities.
async def test_calculate_registered_metric_resolves_targets_from_candidates():
    resp = _ok_response(intent_kind="calculate_registered_metric")
    fake = _fake_complete([resp])
    goal = await parse_intent("посчитай выручку Северстали за 2024 год", complete_fn=fake)
    assert goal.intent_kind == "calculate_registered_metric"
    assert [t.concept for t in goal.targets] == ["SALES_REVENUE"]


async def test_compound_request_keeps_distinct_goal_types_and_dependencies():
    response = _ok_response(
        goal_ref="profit",
        intent_kind="extract_registered_metric",
        targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "margin", "depends_on": ["profit"],
            "intent_kind": "calculate_registered_metric",
            "targets": [{"concept": "SALES_REVENUE"}],
            "entity_ref": "Северсталь", "period_ref": "2024", "role": None,
            "unresolved": [], "ambiguities": [], "analysis_concepts": [],
        }],
    )
    goal = await parse_intent(
        "извлеки чистую прибыль и посчитай выручку за 2024",
        complete_fn=_fake_complete([response]),
    )

    clauses = goal.clauses()
    assert [clause.intent_kind for clause in clauses] == [
        "extract_registered_metric", "calculate_registered_metric",
    ]
    assert clauses[1].depends_on == ["profit"]
    assert root_from_goal(clauses[0])[0].root.output_type == "ExtractedMetric"
    assert root_from_goal(clauses[1])[0].root.output_type == "CalculatedMetric"


async def test_compound_independent_goals_have_no_dependency():
    response = _ok_response(
        goal_ref="profit", intent_kind="extract_registered_metric",
        targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "arithmetic", "depends_on": [],
            "intent_kind": "calculate_ad_hoc_metric", "targets": [],
            "entity_ref": None, "period_ref": None, "role": None,
            "unresolved": ["2+2"], "ambiguities": [], "analysis_concepts": [],
            "ad_hoc_formula": {
                "name_ru": "2+2", "expression": "2 + 2", "components": {},
            },
        }],
    )
    goal = await parse_intent("извлеки прибыль и посчитай 2+2", complete_fn=_fake_complete([response]))
    assert goal.additional_goals[0].depends_on == []


async def test_compound_retry_explains_null_arrays_and_missing_formula():
    live_shape = _ok_response(
        goal_ref="goal-1", targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "goal-2", "depends_on": None,
            "intent_kind": "calculate_ad_hoc_metric", "targets": None,
            "analysis_concepts": None, "ad_hoc_concept": None,
            "ad_hoc_formula": None, "entity_ref": None, "period_ref": None,
            "role": None, "unresolved": None, "ambiguities": None,
            "correction": None,
        }],
    )
    repaired_clause = json.dumps({
        "goal_ref": "goal-2", "depends_on": [],
        "intent_kind": "calculate_ad_hoc_metric", "targets": [],
        "analysis_concepts": [], "ad_hoc_concept": None,
        "ad_hoc_formula": {
            "name_ru": "2+2", "expression": "2 + 2", "components": {},
        },
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [], "correction": None,
    }, ensure_ascii=False)
    fake = _fake_complete([live_shape, repaired_clause])

    goal = await parse_intent("извлеки прибыль и посчитай 2+2", complete_fn=fake)

    assert len(fake.calls) == 2
    assert "Исправь только дополнительную цель номер 2" in fake.calls[1]
    assert "невалидный черновик" in fake.calls[1]
    assert goal.additional_goals[0].parameters["ad_hoc_formula"]["expression"] == "2 + 2"


async def test_additional_clause_repair_still_validates_the_complete_ad_hoc_goal_set():
    query = (
        "Извлеки прочие внеооборотные и оборотные активы, прочие доходы и расходы "
        "за полугодие 2026. Для каждой цифры укажи единицу, период и точный источник в отчёте. "
        "Не подменяй эти статьи похожими метриками из реестра."
    )
    malformed = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[], ad_hoc_concept=None,
        additional_goals=[{
            "goal_ref": "goal-2", "depends_on": None,
            "intent_kind": "extract_ad_hoc_metric", "targets": None,
            "analysis_concepts": None, "ad_hoc_concept": None, "ad_hoc_formula": None,
            "entity_ref": None, "period_ref": None, "role": None,
            "unresolved": None, "ambiguities": None, "correction": None,
        }],
    )
    repaired_additional = _ok_response(
        intent_kind="extract_ad_hoc_metric", targets=[],
        ad_hoc_concept={
            "name_ru": "Прочие доходы", "name_en": "Other income",
            "aliases_ru": [], "aliases_en": [], "definition": "Доходы за период.",
            "hint": "отчёт", "value_kind": "flow",
        },
    )

    def goal(name: str, kind: str) -> dict:
        return {
            "intent_kind": "extract_ad_hoc_metric", "targets": [], "analysis_concepts": [],
            "entity_ref": None, "period_ref": "30.06.2026", "role": None,
            "unresolved": [], "ambiguities": [], "depends_on": [],
            "ad_hoc_concept": {
                "name_ru": name, "name_en": name, "aliases_ru": [], "aliases_en": [],
                "definition": name, "hint": "отчёт", "value_kind": kind,
            },
        }

    repaired_set = json.dumps({"goals": [
        goal("Прочие внеооборотные активы", "stock"),
        goal("Прочие доходы", "flow"),
    ]}, ensure_ascii=False)
    fake = _fake_complete([malformed, repaired_additional, repaired_set])

    parsed = await parse_intent(query, complete_fn=fake)

    assert len(fake.calls) == 3
    assert [item.parameters["ad_hoc_concept"]["name_ru"] for item in parsed.clauses()] == [
        "Прочие внеооборотные активы", "Прочие доходы",
    ]
    assert parsed.additional_goals[0].period_ref == "6M 2026"


async def test_focused_compound_repair_keeps_model_authored_dependency():
    live_shape = _ok_response(
        goal_ref="goal-1", targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "goal-2", "depends_on": None,
            "intent_kind": "calculate_ad_hoc_metric", "targets": None,
            "analysis_concepts": None, "ad_hoc_concept": None,
            "ad_hoc_formula": None, "entity_ref": None, "period_ref": None,
            "role": None, "unresolved": None, "ambiguities": None,
            "correction": None,
        }],
    )
    repaired_clause = json.dumps({
        "goal_ref": "goal-2", "depends_on": ["goal-1"],
        "intent_kind": "calculate_ad_hoc_metric", "targets": [],
        "analysis_concepts": [], "ad_hoc_concept": None,
        "ad_hoc_formula": {
            "name_ru": "Чистая рентабельность", "expression": "NI / REV * 100",
            "components": {"NI": "Чистая прибыль", "REV": "Выручка"},
        },
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [], "correction": None,
    }, ensure_ascii=False)
    described = json.dumps({"concepts": []}, ensure_ascii=False)
    fake = _fake_complete([live_shape, repaired_clause, described])

    goal = await parse_intent(
        "извлеки чистую прибыль и рассчитай рентабельность по этой прибыли",
        complete_fn=fake,
    )

    assert goal.additional_goals[0].depends_on == ["goal-1"]


async def test_calculate_ad_hoc_null_formula_without_reason_is_retried():
    invalid = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula=None, unresolved=[],
    )
    valid = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[], period_ref=None,
        ad_hoc_formula={"name_ru": "2+2", "expression": "2 + 2", "components": {}},
        unresolved=[],
    )
    fake = _fake_complete([invalid, valid])

    goal = await parse_intent("посчитай 2+2", complete_fn=fake)

    assert len(fake.calls) == 2
    assert "ad_hoc_formula=null допустима только" in fake.calls[1]
    assert goal.parameters["ad_hoc_formula"]["expression"] == "2 + 2"


async def test_ad_hoc_metric_kinds_always_leave_targets_empty():
    # Модель нарушила инструкцию и всё равно прислала code — тот же принцип
    # A1.5, что раньше проверялся только для full_analysis/form, теперь
    # покрывает оба *_ad_hoc_metric вида.
    resp = _ok_response(intent_kind="extract_ad_hoc_metric", targets=[{"concept": "SALES_REVENUE"}])
    fake = _fake_complete([resp])
    goal = await parse_intent("покажи что-то нестандартное у Северстали", complete_fn=fake)
    assert goal.intent_kind == "extract_ad_hoc_metric"
    assert goal.targets == []


# ---------------------------------------------------------------------------
# Срез 27/31: calculate_ad_hoc_metric доносит проверенную формулу из ответа
# модели в GoalIR.parameters["ad_hoc_formula"] — мост к calc.formula.
async def test_calculate_ad_hoc_metric_parses_expression_and_dependencies_from_the_model():
    resp = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[],
        ad_hoc_formula={
            "expression": "SALES_REVENUE * 2", "dependencies": ["SALES_REVENUE"],
            "assumptions": "без корректировки на инфляцию",
        },
    )
    fake = _fake_complete([resp])
    goal = await parse_intent("посчитай удвоенную выручку Северстали за 2024 год", complete_fn=fake)
    formula = goal.parameters["ad_hoc_formula"]
    assert formula["expression"] == "SALES_REVENUE * 2"
    assert formula["dependencies"] == ["SALES_REVENUE"]
    assert formula["assumptions"] == "без корректировки на инфляцию"


async def test_calculate_ad_hoc_metric_drops_dependencies_that_are_neither_registry_nor_described():
    # Та же детерминированная проверка ПОСЛЕ ответа модели, что уже отсекает
    # выдуманный enum из targets: символ без описания и без кода реестра не
    # должен тихо попасть в формулу, которую планировщик не разрешит.
    resp = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[],
        ad_hoc_formula={
            "expression": "SALES_REVENUE - FAKE_HALLUCINATED_ENUM",
            "dependencies": ["SALES_REVENUE", "FAKE_HALLUCINATED_ENUM"],
        },
    )
    fake = _fake_complete([resp, resp])  # невалидная формула → одна повторная попытка с причиной
    goal = await parse_intent("посчитай выручку Северстали минус нечто выдуманное", complete_fn=fake)
    assert "ad_hoc_formula" not in goal.parameters
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Retrofit v9 (R3.3/R4): extract и calculate одного concept — разные
# `Target.output_type`, не один и тот же `Target`, разрешаемый через
# `PlanPolicy` tie-break между `read.value`/`calc.formula_engine`.
def test_extract_and_calculate_of_the_same_concept_are_different_targets():
    goal_extract = GoalIR(
        raw_query="q", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")],
        entity_ref="ent", period_ref="2024",
    )
    goal_calculate = GoalIR(
        raw_query="q", intent_kind="calculate_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")],
        entity_ref="ent", period_ref="2024",
    )
    extract_root = root_from_goal(goal_extract)[0].root
    calculate_root = root_from_goal(goal_calculate)[0].root

    assert extract_root.concept_id == calculate_root.concept_id == "SALES_REVENUE"
    assert extract_root.output_type == "ExtractedMetric"
    assert calculate_root.output_type == "CalculatedMetric"
    assert extract_root != calculate_root


# ---------------------------------------------------------------------------
# R4.5: ad-hoc extraction/calculation создают временный `AdHocConceptSpec`/
# `AdHocFormulaSpec` из формулировки пользователя — не пустой список корней
# (см. докстринг R4.5 в `RETROFIT-REPORT-v9.md`: до retrofit ad-hoc виды не
# производили НИ ОДНОГО Target).
def test_root_from_goal_ad_hoc_extract_creates_a_temporary_concept_spec():
    goal = GoalIR(
        raw_query="q", intent_kind="extract_ad_hoc_metric", targets=[],
        entity_ref="ent", period_ref="2024", unresolved=["площадь торговых залов"],
    )
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "ExtractedMetric"
    assert roots[0].root.concept_id == "площадь торговых залов"
    assert isinstance(roots[0].ad_hoc_spec, AdHocConceptSpec)
    assert roots[0].ad_hoc_spec.term == "площадь торговых залов"
    assert roots[0].root_extra["ad_hoc_spec_id"] == roots[0].ad_hoc_spec.spec_id


def test_root_from_goal_ad_hoc_calculate_creates_a_temporary_formula_spec():
    """Срез 27: root_extra теперь несёт реальную expression/dependencies —
    тот же мост, что calc_formula.py::_ad_hoc_spec_from_extra уже читает —
    не только term без формулы (старая, недоисполняемая форма)."""
    formula = AdHocFormulaSpec(
        term="выручка на кв.м. торговой площади", expression="REVENUE / TRADE_AREA",
        dependencies=["REVENUE", "TRADE_AREA"], assumptions="площадь на конец периода",
    )
    goal = GoalIR(
        raw_query="q", intent_kind="calculate_ad_hoc_metric", targets=[],
        entity_ref="ent", period_ref="2024", unresolved=["выручка на кв.м. торговой площади"],
        parameters={"ad_hoc_formula": formula.model_dump(mode="json")},
    )
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "CalculatedMetric"
    assert isinstance(roots[0].ad_hoc_spec, AdHocFormulaSpec)
    assert roots[0].ad_hoc_spec.expression == "REVENUE / TRADE_AREA"
    assert roots[0].ad_hoc_spec.dependencies == ["REVENUE", "TRADE_AREA"]
    assert roots[0].ad_hoc_spec.assumptions == "площадь на конец периода"
    assert roots[0].root_extra["ad_hoc_formula_expression"] == "REVENUE / TRADE_AREA"
    assert roots[0].root_extra["ad_hoc_formula_dependencies"] == "REVENUE,TRADE_AREA"
    assert roots[0].root_extra["ad_hoc_formula_assumptions"] == "площадь на конец периода"


async def test_calculate_ad_hoc_metric_rejects_expression_tokens_outside_dependencies():
    resp = _ok_response(
        intent_kind="calculate_ad_hoc_metric", targets=[],
        ad_hoc_formula={
            "expression": "eval(SALES_REVENUE)", "dependencies": ["SALES_REVENUE"],
            "assumptions": "",
        },
    )
    goal = await parse_intent("посчитай опасную формулу", complete_fn=_fake_complete([resp, resp]))
    assert "ad_hoc_formula" not in goal.parameters


def test_root_from_goal_ad_hoc_calculate_without_a_formula_asks_for_clarification():
    """Без expression/dependencies от intent.parse строить корень, обречённый
    вернуть пустой CalculatedMetric глубоко внутри исполнителя, — угадывание,
    не отказ; правильное поведение — явная просьба уточнить формулу."""
    goal = GoalIR(
        raw_query="q", intent_kind="calculate_ad_hoc_metric", targets=[],
        entity_ref="ent", period_ref="2024", unresolved=["выручка на кв.м. торговой площади"],
    )
    with pytest.raises(ValueError, match="expression"):
        root_from_goal(goal)


def test_ad_hoc_spec_never_gets_written_to_the_atom_registry():
    """R4.5: ad-hoc spec живёт только у вызывающего — реестр атомов остаётся
    неизменным до и после (никакой скрытой регистрации нового producer'а)."""
    from finengine.skills.atoms.graph_atoms import build_registry

    before = build_registry().fingerprint()
    goal = GoalIR(
        raw_query="q", intent_kind="extract_ad_hoc_metric", targets=[],
        entity_ref="ent", period_ref="2024", unresolved=["метрика вне реестра"],
    )
    root_from_goal(goal)
    after = build_registry().fingerprint()
    assert before == after


def test_root_from_goal_raises_not_implemented_for_not_yet_wired_kinds():
    goal = GoalIR(raw_query="q", intent_kind="document_qa", entity_ref="ent", period_ref="2024")
    with pytest.raises(NotImplementedError):
        root_from_goal(goal)


def test_monitor_changes_builds_the_period_change_root_of_one_document():
    """Срез 32: MONITOR_CHANGES не требует ни Case, ни стабильного `subject_key` — он
    сравнивает периоды ОДНОГО отчёта. Период не назван → `all_available`: корень без
    периода, объяснение — по данным документа (без веб-поиска), роль едет в `root_extra`."""
    goal = GoalIR(raw_query="что изменилось?", intent_kind="monitor_changes", role="risks", period_ref="2024")
    (root,) = root_from_goal(goal)
    assert root.root.output_type == "PeriodChangeReport"
    assert root.root.period_ref is None, "изменения между периодами не фильтруются одним периодом"
    assert "document_scope" not in root.root.params
    assert root.root_extra == {"role": "risks", "external_context": "off", "structures": "off"}


def test_root_from_goal_form_carries_role_in_root_extra():
    goal = GoalIR(raw_query="q", intent_kind="form", targets=[], role="dgr", entity_ref="ent", period_ref="2024")
    roots = root_from_goal(goal)
    assert len(roots) == 1
    assert roots[0].root.output_type == "FormOutput"
    assert roots[0].root_extra == {"role": "dgr"}


async def test_custom_analysis_goal_keeps_the_whole_request_in_unresolved_and_drops_targets():
    """`custom_analysis` — обработка сверх каталога атомов: `targets` всегда пусты (модель
    могла положить туда показатели из начала просьбы), просьба уходит в `unresolved` дословно."""
    request = "проведи корреляционный анализ, результат анализа верни в excel"
    fake = _fake_complete([_ok_response(intent_kind="custom_analysis", unresolved=[request])])

    goal = await parse_intent("Извлеки показатели и " + request, complete_fn=fake)

    assert goal.intent_kind == "custom_analysis"
    assert goal.targets == []
    assert goal.unresolved == [request]


def test_custom_analysis_is_a_known_kind_and_unknown_kinds_are_still_rejected():
    assert "custom_analysis" in intent_module._INTENT_KINDS
    with pytest.raises(intent_module._SchemaError):
        intent_module._validate_goal_shape({"intent_kind": "no_such_kind"})


def test_root_from_goal_raises_not_implemented_for_custom_analysis():
    goal = GoalIR(raw_query="q", intent_kind="custom_analysis", entity_ref="ent", period_ref="2024")
    with pytest.raises(NotImplementedError):
        root_from_goal(goal)


def test_prompt_defines_custom_analysis_and_excludes_a_plain_export_to_a_file():
    prompt = intent_module._SYSTEM_PROMPT
    assert '"custom_analysis"' in prompt
    assert prompt.count('"custom_analysis"') >= 2, "и определение вида, и перечень значений в форме ответа"
    assert "Без АНАЛИЗА (статистики, собственного расчёта, внешних данных) это НЕ custom_analysis" in prompt


def test_repair_prompt_keeps_a_custom_analysis_draft_instead_of_turning_it_into_a_formula():
    """Часть 15, А1/А4: ремонт составной цели не знал custom_analysis и подменял его на
    calculate_ad_hoc_metric (КВ → формула, которую схема отвергает; ряд Brent → формула над отчётом)."""
    prompt = intent_module._COMPOUND_CLAUSE_REPAIR_SYSTEM

    assert "custom_analysis" in prompt, "ремонт знает вид: без этого он подменяет его на знакомый"
    assert "темпы роста" not in prompt, "формула (B-A)/A выразима: темп роста остаётся calculate_ad_hoc_metric"


async def test_a_null_placeholder_custom_analysis_clause_is_repaired_with_the_custom_analysis_rule():
    seen: list[tuple[str, str]] = []
    main = json.dumps({
        "route": "plan", "goal_ref": "goal-1", "depends_on": [], "intent_kind": "extract_registered_metric",
        "targets": [{"concept": "SALES_REVENUE"}], "analysis_concepts": [], "entity_ref": None,
        "period_ref": None, "role": None, "unresolved": [], "ambiguities": [],
        "additional_goals": [{
            "goal_ref": "goal-2", "depends_on": None, "intent_kind": "custom_analysis", "targets": None,
            "analysis_concepts": None, "ad_hoc_formula": None, "unresolved": None, "ambiguities": None,
        }],
    }, ensure_ascii=False)
    clause = json.dumps({
        "goal_ref": "goal-2", "depends_on": ["goal-1"], "intent_kind": "custom_analysis", "targets": [],
        "analysis_concepts": [], "ad_hoc_concept": None, "ad_hoc_formula": None, "entity_ref": None,
        "period_ref": None, "role": None, "unresolved": ["посчитай коэффициент вариации"],
        "ambiguities": [], "correction": None,
    }, ensure_ascii=False)
    responses = [main, clause]

    async def complete(system: str, prompt: str, *, model_version: str | None = None) -> str:
        seen.append((system, prompt))
        return responses[len(seen) - 1]

    goal = await intent_module.parse_message(
        "извлеки выручку и посчитай для неё коэффициент вариации", complete_fn=complete,
    )

    repair_system, repair_prompt = seen[1]
    assert len(seen) == 2, "один основной вызов и один ремонт цели"
    assert "custom_analysis" in repair_system
    assert '"intent_kind": "custom_analysis"' in repair_prompt, "модель видит вид черновика"
    assert [item.intent_kind for item in goal.goal.clauses()] == ["extract_registered_metric", "custom_analysis"]
    assert goal.goal.additional_goals[0].depends_on == ["goal-1"]
