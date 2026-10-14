"""Срез 31 — `intent.parse` как маршрутизатор ВСЕХ сообщений (П3), период по
умолчанию «все» (П6), роль из UI (П2), ad-hoc понятия и формулы от модели (П4/П5).

Критерии: docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-ingress-v2.md §4.
Модельная граница подменяется `complete_fn` (как в `test_intent_parse.py`).
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec
from finengine.skills.atoms.intent import (
    IngressContext,
    IntentDecision,
    IntentParseError,
    parse_intent,
    parse_message,
    root_from_goal,
)


def _fake(responses: list[dict | str]):
    calls: list[dict] = []

    async def _complete(system: str, prompt: str, *, model_version: str | None = None) -> str:
        calls.append({"system": system, "prompt": prompt})
        item = responses[min(len(calls), len(responses)) - 1]
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)

    _complete.calls = calls  # type: ignore[attr-defined]
    return _complete


def _plan(**overrides) -> dict:
    base = {
        "route": "plan", "intent_kind": "extract_registered_metric",
        "targets": [{"concept": "SALES_REVENUE"}], "entity_ref": None, "period_ref": None,
        "role": None, "unresolved": [], "ambiguities": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# П3: маршрут
async def test_the_model_can_answer_on_its_own_and_no_goal_is_built():
    fake = _fake([{"route": "answer", "intent_kind": None, "targets": [], "reason": "приветствие"}])
    decision = await parse_message("привет, что ты умеешь?", complete_fn=fake)
    assert isinstance(decision, IntentDecision)
    assert decision.route == "answer" and decision.goal is None
    assert decision.reason == "приветствие"


async def test_a_plan_route_returns_the_goal_for_the_planner():
    decision = await parse_message("покажи выручку", complete_fn=_fake([_plan()]))
    assert decision.route == "plan" and decision.goal is not None
    assert decision.goal.intent_kind == "extract_registered_metric"
    assert [t.concept for t in decision.goal.targets] == ["SALES_REVENUE"]


async def test_a_missing_route_means_plan_for_backward_compatible_payloads():
    payload = _plan()
    del payload["route"]
    decision = await parse_message("покажи выручку", complete_fn=_fake([payload]))
    assert decision.route == "plan"


async def test_an_unknown_route_is_an_invalid_schema_and_is_retried_once():
    fake = _fake([_plan(route="maybe"), _plan()])
    decision = await parse_message("покажи выручку", complete_fn=fake)
    assert decision.route == "plan" and len(fake.calls) == 2
    assert "route" in fake.calls[1]["prompt"], "повторный запрос называет причину невалидности"


async def test_the_model_failing_twice_is_an_explicit_error_not_a_silent_answer_route():
    with pytest.raises(IntentParseError):
        await parse_message("привет", complete_fn=_fake(["не json", "тоже не json"]))


async def test_parse_intent_keeps_working_for_callers_that_cannot_accept_an_answer_route():
    """Станционный CLI обязан получить цель: route="answer" для него невалиден."""
    fake = _fake([{"route": "answer", "intent_kind": None, "targets": []}, _plan()])
    goal = await parse_intent("покажи выручку", complete_fn=fake)
    assert goal.intent_kind == "extract_registered_metric"
    assert len(fake.calls) == 2
    assert "ВСЕГДА" in fake.calls[0]["system"] and "plan" in fake.calls[0]["system"], (
        "вызов без маршрутизации сообщает модели, что ответ «сам» не поддерживается"
    )


async def test_the_system_prompt_obliges_the_model_to_hand_goal_type_requests_to_the_planner():
    """Критерий T3.3: правило, о котором просил владелец, живёт в промпте."""
    fake = _fake([_plan()])
    await parse_message("покажи выручку", complete_fn=fake)
    system = fake.calls[0]["system"]
    assert 'route="plan"' in system and 'route="answer"' in system
    assert "ОБЯЗАН" in system and "планировщику" in system
    assert "отвечать на него самому нельзя" in system


# ---------------------------------------------------------------------------
# Контекст диалога
async def test_context_is_shown_to_the_model_documents_role_and_recent_turns():
    fake = _fake([_plan()])
    ctx = IngressContext(
        documents=(("ЛУКОЙЛ 1П2026.pdf", True), ("Северсталь.pdf", False)),
        ui_role="dgr",
        history=(("user", "покажи выручку"), ("assistant", "Выручка 1 000 млн"), ("user", "а за 2025?")),
    )
    await parse_message("и по прибыли тоже", context=ctx, complete_fn=fake)
    prompt = fake.calls[0]["prompt"]
    assert "ЛУКОЙЛ 1П2026.pdf" in prompt and "активный" in prompt
    assert "Северсталь.pdf" in prompt
    assert "ДГР" in prompt and "dgr" in prompt
    assert "Пользователь: покажи выручку" in prompt and "Ассистент: Выручка 1 000 млн" in prompt


async def test_without_context_the_prompt_has_no_dialogue_block():
    fake = _fake([_plan()])
    await parse_message("покажи выручку", complete_fn=fake)
    assert "Контекст диалога" not in fake.calls[0]["prompt"]


async def test_a_short_followup_finds_its_metric_candidates_in_the_previous_user_turn():
    """«а за 2025?» само показателя не называет — кандидаты берутся из прошлой реплики."""
    fake = _fake([_plan(period_ref="2025")])
    ctx = IngressContext(history=(("user", "покажи выручку"), ("assistant", "…")))
    decision = await parse_message("а за 2025?", context=ctx, complete_fn=fake)
    assert "SALES_REVENUE" in fake.calls[0]["prompt"]
    assert decision.goal is not None and [t.concept for t in decision.goal.targets] == ["SALES_REVENUE"]
    assert decision.goal.period_ref == "2025"


# ---------------------------------------------------------------------------
# П6: период
async def test_no_period_named_means_all_periods_and_nothing_to_clarify():
    decision = await parse_message("извлеки выручку", complete_fn=_fake([_plan(period_ref=None)]))
    goal = decision.goal
    assert goal is not None and goal.period_ref is None
    assert not goal.ambiguities and not goal.unresolved, "отсутствие периода — не неоднозначность"
    roots = root_from_goal(goal)
    assert roots[0].root.period_ref is None, "корень без периода: экстрактор возвращает все доступные"


async def test_a_period_invented_by_the_model_is_dropped_so_all_periods_are_used():
    decision = await parse_message("извлеки выручку", complete_fn=_fake([_plan(period_ref="2019")]))
    assert decision.goal is not None and decision.goal.period_ref is None


async def test_a_period_named_by_the_user_is_kept():
    decision = await parse_message("выручка за 2024 год", complete_fn=_fake([_plan(period_ref="2024")]))
    assert decision.goal is not None and decision.goal.period_ref == "2024"
    assert root_from_goal(decision.goal)[0].root.period_ref == "2024"


async def test_a_period_carried_by_a_followup_from_the_previous_user_turn_stays_grounded():
    ctx = IngressContext(history=(("user", "выручка за 2024 год"), ("assistant", "…")))
    decision = await parse_message("а по прибыли?", context=ctx, complete_fn=_fake([_plan(period_ref="2024")]))
    assert decision.goal is not None and decision.goal.period_ref == "2024"


async def test_the_prompt_forbids_asking_for_a_period():
    fake = _fake([_plan()])
    await parse_message("покажи выручку", complete_fn=fake)
    system = fake.calls[0]["system"]
    assert "ВСЕ доступные периоды" in system
    assert "не проси уточнить" in system


# ---------------------------------------------------------------------------
# П2: роль из интерфейса
async def test_the_ui_role_becomes_the_role_of_the_goal():
    ctx = IngressContext(ui_role="dgr")
    decision = await parse_message(
        "сделай полный анализ", context=ctx, complete_fn=_fake([_plan(intent_kind="full_analysis", targets=[])]),
    )
    assert decision.goal is not None and decision.goal.role == "dgr"


async def test_the_ui_role_beats_a_different_role_guessed_from_the_request():
    ctx = IngressContext(ui_role="dgr")
    decision = await parse_message(
        "сделай полный анализ для рисков", context=ctx,
        complete_fn=_fake([_plan(intent_kind="full_analysis", targets=[], role="risks")]),
    )
    assert decision.goal is not None and decision.goal.role == "dgr"


async def test_a_form_request_is_not_ambiguous_when_the_ui_role_is_chosen():
    ctx = IngressContext(ui_role="cib")
    decision = await parse_message(
        "собери форму", context=ctx, complete_fn=_fake([_plan(intent_kind="form", targets=[])]),
    )
    assert decision.goal is not None and decision.goal.role == "cib"
    assert not decision.goal.ambiguities


@pytest.mark.parametrize("ui_role", ["", "нет-такой-роли"])
async def test_an_absent_or_unknown_ui_role_leaves_the_role_empty(ui_role):
    decision = await parse_message(
        "покажи выручку", context=IngressContext(ui_role=ui_role), complete_fn=_fake([_plan()]),
    )
    assert decision.goal is not None and decision.goal.role is None


# ---------------------------------------------------------------------------
# П5: ad-hoc извлечение — описание придумывает модель
async def test_extract_ad_hoc_carries_the_name_aliases_and_definition_invented_by_the_model():
    payload = _plan(
        intent_kind="extract_ad_hoc_metric", targets=[], unresolved=["средневзвешенная ставка по кредитам"],
        ad_hoc_concept={
            "name_ru": "Средневзвешенная ставка по кредитам и займам",
            "name_en": "Weighted average interest rate on borrowings",
            "aliases_ru": ["средняя ставка по кредитам", "средневзвешенная процентная ставка"],
            "aliases_en": ["weighted average interest rate"],
            "definition": "Средняя процентная ставка по долговым обязательствам.",
            "hint": "Примечание о кредитах и займах, проценты годовых.", "value_kind": "ratio",
        },
    )
    decision = await parse_message("покажи среднюю ставку по кредитам", complete_fn=_fake([payload]))
    goal = decision.goal
    assert goal is not None and goal.intent_kind == "extract_ad_hoc_metric" and goal.targets == []

    roots = root_from_goal(goal)
    assert len(roots) == 1
    spec = roots[0].ad_hoc_spec
    assert isinstance(spec, AdHocConceptSpec)
    assert spec.name_ru == "Средневзвешенная ставка по кредитам и займам"
    assert "средняя ставка по кредитам" in spec.aliases_ru
    assert spec.definition.startswith("Средняя процентная ставка")
    assert roots[0].root.concept_id == "Средневзвешенная ставка по кредитам и займам"
    assert roots[0].root_extra["ad_hoc_spec_id"] == spec.spec_id
    assert "ad_hoc_concepts" in roots[0].root_extra, "описание доезжает до узлов графа через NodeArgs.extra"


async def test_extract_ad_hoc_without_a_model_description_still_works_from_the_users_phrase():
    payload = _plan(intent_kind="extract_ad_hoc_metric", targets=[], unresolved=["площадь торговых залов"])
    decision = await parse_message("покажи площадь торговых залов", complete_fn=_fake([payload]))
    assert decision.goal is not None
    roots = root_from_goal(decision.goal)
    assert roots[0].root.concept_id == "площадь торговых залов"


# ---------------------------------------------------------------------------
# П4: расчёт без формулы от пользователя — формулу выдаёт модель
async def test_calculate_ad_hoc_builds_a_root_from_the_formula_the_model_invented():
    payload = _plan(
        intent_kind="calculate_ad_hoc_metric", targets=[], unresolved=["чистый долг к EBITDA"],
        ad_hoc_formula={
            "name_ru": "Чистый долг к EBITDA", "unit": "x",
            "expression": "(DEBT_X - CASH_Y) / EBITDA_Z",
            "components": [
                {"key": "DEBT_X", "name_ru": "Долг-икс тестовый", "aliases_ru": ["кредитный пакет-икс"]},
                {"key": "CASH_Y", "name_ru": "Касса-игрек тестовая"},
                {"key": "EBITDA_Z", "name_ru": "Показатель-зет тестовый"},
            ],
            "assumptions": "Долг включает краткосрочную часть",
        },
    )
    decision = await parse_message("посчитай чистый долг к EBITDA", complete_fn=_fake([payload]))
    assert decision.goal is not None and decision.goal.intent_kind == "calculate_ad_hoc_metric"

    roots = root_from_goal(decision.goal)
    assert len(roots) == 1 and roots[0].root.output_type == "CalculatedMetric"
    spec = roots[0].ad_hoc_spec
    assert isinstance(spec, AdHocFormulaSpec)
    assert spec.expression == "(DEBT_X - CASH_Y) / EBITDA_Z"
    assert set(spec.dependencies) == {"DEBT_X", "CASH_Y", "EBITDA_Z"}
    assert spec.assumptions == "Долг включает краткосрочную часть"
    assert roots[0].root.concept_id == "Чистый долг к EBITDA"
    assert roots[0].root_extra["ad_hoc_formula_unit"] == "x"
    assert roots[0].root_extra["ad_hoc_formula_dependencies"] == "DEBT_X,CASH_Y,EBITDA_Z"


async def test_an_untrusted_formula_gives_no_root_instead_of_a_guessing_root():
    payload = _plan(
        intent_kind="calculate_ad_hoc_metric", targets=[],
        ad_hoc_formula={"expression": "A / B", "components": [{"key": "A", "name_ru": "а тестовое"}]},
    )
    decision = await parse_message("посчитай что-то", complete_fn=_fake([payload]))
    assert decision.goal is not None
    with pytest.raises(ValueError, match="формул"):
        root_from_goal(decision.goal)


# ---------------------------------------------------------------------------
# Живая проверка (M2, dev, 24.09.2026) показала: реальная модель вернула
# `"components": [null, null]` — схема заполнена пустышками, формула молча
# отбрасывалась, а пользователь получал отказ. Теперь невалидная формула — это
# невалидная схема ответа: модели возвращают КОНКРЕТНУЮ причину и дают ОДНУ
# повторную попытку.
_BAD_FORMULA = {
    "name_ru": "Доля краткосрочных обязательств", "unit": "%", "expression": "A / B * 100",
    "assumptions": "", "components": [None, None],
}
_GOOD_FORMULA = {
    "name_ru": "Доля краткосрочных обязательств", "unit": "%", "expression": "SHORT / TOTAL * 100",
    "components": [
        {"key": "SHORT", "name_ru": "Краткосрочные обязательства тестовые", "value_kind": "stock"},
        {"key": "TOTAL", "name_ru": "Обязательства всего тестовые", "value_kind": "stock"},
    ],
}


async def test_an_invalid_formula_is_retried_once_with_the_concrete_reason():
    fake = _fake([
        _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=_BAD_FORMULA),
        _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=_GOOD_FORMULA),
    ])

    decision = await parse_message("посчитай долю краткосрочных обязательств", complete_fn=fake)

    assert len(fake.calls) >= 2, "одна повторная попытка (плюс описание компонентов)"
    assert "ad_hoc_formula" in fake.calls[1]["prompt"] and "компонент" in fake.calls[1]["prompt"], (
        "модели названа причина, а не просто «невалидно»"
    )
    assert decision.goal is not None and "ad_hoc_formula" in decision.goal.parameters


@pytest.mark.parametrize("formula, reason", [
    ({"expression": "A / B", "components": [None]}, "компонент"),
    ({"expression": "A / B", "components": []}, "components"),
    ({"expression": "eval(A)", "components": [{"key": "A", "name_ru": "а тестовое"}]}, "expression"),
    ({"expression": "A / B", "components": [{"key": "A", "name_ru": "а тестовое"}]}, "символ"),
    ({"expression": "ДОЛГ / КАПИТАЛ", "components": [{"key": "ДОЛГ"}, {"key": "КАПИТАЛ"}]}, "expression"),
])
async def test_the_reason_for_a_rejected_formula_names_what_is_wrong(formula, reason):
    payload = _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=formula)
    fake = _fake([payload, payload])

    decision = await parse_message("посчитай что-то", complete_fn=fake)

    assert reason in fake.calls[1]["prompt"], "причина возвращена модели"
    assert len(fake.calls) == 2
    assert decision.goal is not None and "ad_hoc_formula" not in decision.goal.parameters, (
        "модель упорствует — цель без формулы, а не общий отказ разбора"
    )


async def test_a_null_formula_is_the_models_honest_refusal_and_is_not_retried():
    fake = _fake([_plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=None, unresolved=["нечто"])])
    decision = await parse_message("посчитай нечто", complete_fn=fake)
    assert len(fake.calls) == 1 and decision.goal is not None
    assert "ad_hoc_formula" not in decision.goal.parameters


async def test_the_prompt_shows_a_filled_formula_example_with_a_flat_components_map():
    fake = _fake([_plan()])
    await parse_message("покажи выручку", complete_fn=fake)
    system = fake.calls[0]["system"]
    assert "ПРИМЕР ad_hoc_formula" in system
    assert '"components": {"REV": "Выручка", "INV": "Запасы"}' in system, (
        "пример показывает простое отображение «символ → название», а не вложенный массив объектов"
    )
    assert "не копируй из примера" in system, "живая модель переносила значения из примера в ответ (M2)"
    assert "Описание понятий (синонимы, определение) не нужно" in system


# ---------------------------------------------------------------------------
# Компоненты формулы, которых нет в реестре, дополняются ОТДЕЛЬНЫМ небольшим
# вызовом модели (синонимы, определение, подсказка) — тот же принцип, что для
# ad-hoc показателя. Ответ модели с формулой остаётся простым.
_FLAT_FORMULA = {
    "name_ru": "Доля краткосрочных обязательств", "unit": "%", "expression": "SHORT / TOTAL * 100",
    "components": {"SHORT": "Краткосрочные обязательства тестовые", "TOTAL": "Обязательства всего тестовые"},
}


def _is_describe_call(system: str) -> bool:
    return "описание понятий" in system


def _routing_fake(describe_payload: dict | str | None, plan_payload: dict):
    calls: list[dict] = []

    async def _complete(system: str, prompt: str, *, model_version: str | None = None) -> str:
        calls.append({"system": system, "prompt": prompt})
        if _is_describe_call(system):
            if describe_payload is None:
                raise RuntimeError("модель описания недоступна")
            return describe_payload if isinstance(describe_payload, str) else json.dumps(describe_payload, ensure_ascii=False)
        return json.dumps(plan_payload, ensure_ascii=False)

    _complete.calls = calls  # type: ignore[attr-defined]
    return _complete


async def test_components_missing_from_the_registry_are_described_by_a_second_small_call():
    fake = _routing_fake(
        {"concepts": [
            {"name_ru": "Краткосрочные обязательства тестовые", "name_en": "Current liabilities test",
             "aliases_ru": ["краткосрочные долги"], "aliases_en": ["current liabilities"],
             "definition": "Обязательства со сроком до года.", "hint": "Баланс, раздел V.", "value_kind": "stock"},
            {"name_ru": "Обязательства всего тестовые", "aliases_ru": ["итого обязательства"],
             "definition": "Сумма всех обязательств.", "value_kind": "stock"},
        ]},
        _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=_FLAT_FORMULA),
    )

    decision = await parse_message("посчитай долю краткосрочных обязательств", complete_fn=fake)

    assert len(fake.calls) == 2 and _is_describe_call(fake.calls[1]["system"])
    assert "Краткосрочные обязательства тестовые" in fake.calls[1]["prompt"]
    formula = AdHocFormulaSpec.model_validate(decision.goal.parameters["ad_hoc_formula"])
    by_key = {c.concept_key: c for c in formula.components}
    assert by_key["SHORT"].aliases_ru == ["краткосрочные долги"] and by_key["SHORT"].value_kind == "stock"
    assert by_key["TOTAL"].definition == "Сумма всех обязательств."


async def test_a_registry_component_is_not_sent_to_the_describer():
    """`Выручка` совпадает с реестром по точному имени — описывать нечего."""
    formula = {"name_ru": "Доля", "expression": "A / REV", "components": {"A": "Икс тестовый", "REV": "Выручка"}}
    fake = _routing_fake({"concepts": [{"name_ru": "Икс тестовый", "aliases_ru": ["икс"]}]},
                         _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=formula))

    await parse_message("посчитай что-то", complete_fn=fake)

    describe_prompt = fake.calls[1]["prompt"]
    assert "Икс тестовый" in describe_prompt and "Выручка" not in describe_prompt


async def test_no_second_call_when_every_component_is_already_described_or_in_the_registry():
    formula = {
        "expression": "A / B", "components": {
            "A": {"name_ru": "Икс тестовый", "aliases_ru": ["икс"], "definition": "опр"},
            "B": {"name_ru": "Игрек тестовый", "aliases_en": ["y"]},
        },
    }
    fake = _routing_fake(None, _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=formula))
    await parse_message("посчитай что-то", complete_fn=fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("describe_payload", [None, "не json", {"concepts": "нет"}, {"concepts": [None]}])
async def test_a_failing_describer_never_breaks_the_parse(describe_payload):
    fake = _routing_fake(describe_payload, _plan(intent_kind="calculate_ad_hoc_metric", targets=[], ad_hoc_formula=_FLAT_FORMULA))

    decision = await parse_message("посчитай долю", complete_fn=fake)

    formula = AdHocFormulaSpec.model_validate(decision.goal.parameters["ad_hoc_formula"])
    assert {c.concept_key for c in formula.components} == {"SHORT", "TOTAL"}, "формула осталась — компоненты ищутся по названию"
