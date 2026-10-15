"""Срез 31 — Ingress v2 через настоящий `FinengineAgent.query_stream`.

Критерии: docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-ingress-v2.md §4
(П2 роль, П3 маршрут, П4/П5 ad-hoc от модели, П6 период). Модельная граница
подменяется, как в проде: `intent.parse` и пара `locate`/`read.value`;
диспетчерская модель — третья отдельная подмена.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.contracts import ScalarFact
from finengine.orchestrator import agent as ag

from ingress_support import (  # noqa: F401 — `gate_env`: autouse-фикстура окружения
    DOC,
    OWNER,
    RAW_TEXT_MARKER,
    FakeMeta,
    FakeStore,
    Turn,
    gate_env,
    patched_build_registry,
    set_intent,
    set_intent_sequence,
)

_ANSWER = {"route": "answer", "intent_kind": None, "targets": [], "reason": "разговор по контексту"}


def _plan(**overrides) -> dict:
    base = {
        "route": "plan", "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": None, "role": None, "unresolved": [], "ambiguities": [],
    }
    base.update(overrides)
    return base


def _types(events: list[dict]) -> list[str]:
    return [e.get("type", "") for e in events]


def _pipeline_called(events: list[dict]) -> bool:
    return any(e.get("type") == "tool_call" and e.get("name") == "financial_pipeline" for e in events)


# ---------------------------------------------------------------------------
# П3: парсер на КАЖДОЕ сообщение, решение принимает модель
@pytest.mark.parametrize("doc_ids", [(), (DOC,), (DOC, "doc-second")], ids=["no-docs", "one-doc", "two-docs"])
def test_the_parser_runs_exactly_once_for_every_document_situation(monkeypatch, doc_ids):
    # Второй документ существует только в списке сессии: без этой подмены ход ждал бы
    # его распознавания (до 5 минут) и до разбора не дошёл бы.
    monkeypatch.setattr("finengine.store.blobs.document_text_exists", lambda _doc_id: True)
    recorder = set_intent_sequence(monkeypatch, [_ANSWER])
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("привет, что ты умеешь?", doc_ids=doc_ids)

    assert len(recorder.calls) == 1, "модель-разборщик вызывается ровно один раз на сообщение"
    assert turn.dispatcher_calls == 1
    assert "plan_graph" not in _types(events) and not _pipeline_called(events)


def test_an_answer_route_goes_to_the_dispatcher_with_the_document_context(monkeypatch):
    set_intent(monkeypatch, _ANSWER)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("на каком языке этот отчёт?")

    assert turn.dispatcher_calls == 1
    assert RAW_TEXT_MARKER in turn.prompt, "модель отвечает по контексту — документ ей доступен"
    assert not any(e.get("type") == "typed_error" for e in events)


def test_a_plan_route_without_a_document_is_a_typed_refusal_not_a_dispatcher_bypass(monkeypatch):
    set_intent(monkeypatch, _plan())
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку", doc_ids=())

    assert turn.dispatcher_calls == 0 and not _pipeline_called(events)
    assert any(e.get("type") == "typed_error" and e["error"]["code"] == "document_required" for e in events)


def test_a_pinned_tool_turn_still_passes_through_the_model_parser(monkeypatch):
    recorder = set_intent_sequence(monkeypatch, [_ANSWER])
    seen: dict = {}

    async def _model(**kw):
        seen.update(kw)
        yield {"type": "done", "usage": {}}

    monkeypatch.setattr(ag, "_build_context", lambda *_a, **_kw: (RAW_TEXT_MARKER, []))
    monkeypatch.setattr(ag.llm, "stream_events", _model)

    async def _go():
        agent = ag.FinengineAgent()
        return [ev async for ev in agent.query_stream(
            "Извлечь показатели", doc_ids=[DOC], auth_user_id=OWNER, owner_id=OWNER,
            project_id="p-lukoil", pinned_tool="fe_extract_metrics",
        )]

    asyncio.run(_go())
    assert len(recorder.calls) == 1, "абсолютно каждое сообщение сначала проходит intent.parse"
    assert seen.get("pinned_tool") == "fe_extract_metrics"


def test_a_plan_route_with_two_documents_and_no_active_one_fails_closed(monkeypatch):
    monkeypatch.setattr("finengine.store.blobs.document_text_exists", lambda _doc_id: True)
    set_intent(monkeypatch, _plan())
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("сравни выручку", doc_ids=(DOC, "doc-second"))

    assert turn.dispatcher_calls == 0 and not _pipeline_called(events)
    assert any(
        e.get("type") == "typed_error" and e["error"]["code"] == "document_scope_ambiguous"
        for e in events
    )


def test_a_parse_failure_with_an_attached_document_fails_closed(monkeypatch):
    async def _garbage(system, prompt, *, model_version=None):
        return "это не JSON"

    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _garbage)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("какая выручка?")

    assert any(e.get("type") == "typed_error" and e["error"]["code"] == "intent_parse_failed" for e in events)
    assert turn.dispatcher_calls == 0, "маршрут неизвестен — сырой документ диспетчеру не отдаётся"
    assert not any(e.get("type") == "text_delta" for e in events)


def test_a_parse_failure_without_a_document_still_lets_the_dispatcher_talk(monkeypatch):
    async def _garbage(system, prompt, *, model_version=None):
        return "это не JSON"

    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _garbage)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("привет", doc_ids=())

    assert turn.dispatcher_calls == 1 and not any(e.get("type") == "typed_error" for e in events)


def test_a_short_followup_is_understood_from_the_dialogue_history(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    recorder = set_intent_sequence(monkeypatch, [_plan(), _plan(period_ref="2025")])
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 100.0})
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    first = turn.run("покажи выручку")
    session_id = first[0]["session_id"]
    turn.run("а за 2025?", session_id=session_id)

    second_prompt = recorder.calls[1]["prompt"]
    assert "Пользователь: покажи выручку" in second_prompt
    assert "SALES_REVENUE" in second_prompt, "кандидаты берутся и из прошлой реплики пользователя"


def test_a_typed_refusal_puts_the_real_reason_into_the_history_the_next_parse_sees(monkeypatch):
    recorder = set_intent_sequence(monkeypatch, [
        _plan(targets=[], ambiguities=[{"term": "прибыль", "candidates": ["NET_INCOME", "EBITDA_MARGIN"], "reason": "?"}]),
        _plan(),
    ])
    turn = Turn()
    turn.arm(monkeypatch)

    first = turn.run("Извлеки прибыль")
    assert any(e.get("type") == "typed_error" and e["error"]["code"] == "goal_needs_clarification" for e in first)
    session_id = first[0]["session_id"]
    turn.run("чистая", session_id=session_id)

    second_prompt = recorder.calls[1]["prompt"]
    assert "прибыль (NET_INCOME, EBITDA_MARGIN)" in second_prompt, (
        "в истории для разборщика — реальная причина отказа, а не общая фраза"
    )


# ---------------------------------------------------------------------------
# П6: период не спрашивается, берутся все доступные
def _two_period_read_value(concept_values: dict[str, tuple[tuple[str, float], ...]]):
    def _read_value(items, ctx):  # noqa: ARG001
        return {
            args.concept: tuple(
                ScalarFact(
                    produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                    source_ref=f"fake:{args.concept}:{period}", concept=args.concept,
                    period_ref=period, value=value, unit="RUB",
                )
                for period, value in concept_values.get(args.concept, ())
            )
            for args in items
        }
    return _read_value


def test_no_period_named_takes_every_available_period_and_publishes_each_value(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    set_intent(monkeypatch, _plan(period_ref=None))
    locate_exec, _ = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    patched_build_registry(monkeypatch, locate_exec, _two_period_read_value({
        "SALES_REVENUE": (("6M 2025", 100.0), ("6M 2026", 200.0)),
    }))
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("извлеки выручку")

    assert not any(e.get("type") == "typed_error" for e in events), "период не спрашивается"
    typed = [e["result"] for e in events if e.get("type") == "typed_result"]
    assert sorted(r["period_ref"] for r in typed) == ["6M 2025", "6M 2026"], "публикуются ВСЕ найденные даты"
    graph = next(e for e in events if e.get("type") == "plan_graph")
    read_value_nodes = [n for n in graph["nodes"] if n["node_key"]["atom_name"] == "read.value"]
    assert read_value_nodes and all("period=" not in n["node_key"]["args_fingerprint"] for n in read_value_nodes), (
        "корень построен без периода"
    )
    prompt_digits = turn.prompt.replace(" ", "")
    assert "100.0" in prompt_digits and "200.0" in prompt_digits, "диспетчер видит значения всех периодов"
    assert RAW_TEXT_MARKER not in turn.prompt


def test_the_unwanted_period_question_never_appears_for_any_goal_type_without_a_period(monkeypatch):
    """Даже форма без периода не превращается в «уточните период»."""
    set_intent(monkeypatch, _plan(intent_kind="form", targets=[], role="dgr"))
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("заполни форму ДГР")

    for event in events:
        if event.get("type") == "typed_error":
            assert "период" not in event["error"]["message"].lower(), event


# ---------------------------------------------------------------------------
# П2: роль из интерфейса доезжает до цели и корня графа
def _capture_production_goal(monkeypatch) -> dict:
    from finengine.skills import production_runtime as pr
    from finengine.skills.result_events import typed_error_event

    seen: dict = {}

    def _fake(context, goal_ir, **kw):
        seen["goal"] = goal_ir
        seen["root_extra"] = dict(kw.get("root_extra") or {})
        seen["roots"] = kw.get("roots")
        return pr.ProductionOutcome(context=context, refusal_event=typed_error_event(code="captured", message="captured"))

    monkeypatch.setattr(pr, "execute_production_goal", _fake)
    return seen


def test_the_ui_role_reaches_the_full_analysis_goal_and_its_root(monkeypatch):
    seen = _capture_production_goal(monkeypatch)
    set_intent(monkeypatch, _plan(intent_kind="full_analysis", targets=[]))
    turn = Turn()
    turn.arm(monkeypatch)

    turn.run("сделай полный анализ", role="dgr")

    assert seen["goal"].role == "dgr"
    assert seen["root_extra"]["role"] == "dgr"
    assert seen["roots"][0].root.output_type == "ReportOutput"


def test_the_selected_ui_role_beats_a_different_role_in_the_message(monkeypatch):
    seen = _capture_production_goal(monkeypatch)
    set_intent(monkeypatch, _plan(intent_kind="full_analysis", targets=[], role="risks"))
    turn = Turn()
    turn.arm(monkeypatch)

    turn.run("сделай полный анализ для рисков", role="dgr")

    assert seen["goal"].role == "dgr" and seen["root_extra"]["role"] == "dgr"


def test_a_form_request_uses_the_ui_role_and_is_not_ambiguous(monkeypatch):
    seen = _capture_production_goal(monkeypatch)
    set_intent(monkeypatch, _plan(intent_kind="form", targets=[], role=None))
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("собери форму", role="cib")

    assert not any(e.get("type") == "typed_error" and e["error"]["code"] == "goal_needs_clarification" for e in events)
    assert seen["goal"].role == "cib" and seen["root_extra"]["role"] == "cib"
    assert seen["root_extra"]["period_scope"] == "all_available"


def test_compound_clauses_reach_production_as_separate_roots(monkeypatch):
    seen = _capture_production_goal(monkeypatch)
    set_intent(monkeypatch, _plan(
        goal_ref="profit",
        targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "arithmetic", "depends_on": [],
            "intent_kind": "calculate_ad_hoc_metric", "targets": [],
            "analysis_concepts": [], "ad_hoc_concept": None,
            "ad_hoc_formula": {
                "name_ru": "Сумма двух и двух", "expression": "2 + 2", "components": {},
            },
            "entity_ref": None, "period_ref": None, "role": None,
            "unresolved": [], "ambiguities": [], "correction": None,
        }],
    ))
    turn = Turn()
    turn.arm(monkeypatch)

    turn.run("извлеки чистую прибыль и посчитай 2+2")

    assert [root.goal_ref for root in seen["roots"]] == ["profit", "arithmetic"]
    assert [root.root.output_type for root in seen["roots"]] == [
        "ExtractedMetric", "CalculatedMetric",
    ]


def test_compound_formula_reuses_a_fresh_extraction_root_without_node_args_conflict(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    """Регрессия live-дефекта: NET_INCOME одновременно является корнем
    первого сценария и компонентом ad-hoc формулы второго. На свежем Case
    оба подграфа строят один ``locate(NET_INCOME)``; metadata формулы не
    должна превращать общий узел в конфликтующие ``NodeArgs``."""
    set_intent(monkeypatch, _plan(
        goal_ref="profit",
        targets=[{"concept": "NET_INCOME"}],
        additional_goals=[{
            "goal_ref": "margin", "depends_on": ["profit"],
            "intent_kind": "calculate_ad_hoc_metric", "targets": [],
            "analysis_concepts": [], "ad_hoc_concept": None,
            "ad_hoc_formula": {
                "name_ru": "Рентабельность", "unit": "%",
                "expression": "NET_INCOME / SALES_REVENUE * 100",
                "components": [
                    {"key": "NET_INCOME", "registry_code": "NET_INCOME",
                     "name_ru": "Чистая прибыль"},
                    {"key": "SALES_REVENUE", "registry_code": "SALES_REVENUE",
                     "name_ru": "Выручка"},
                ],
            },
            "entity_ref": None, "period_ref": None, "role": None,
            "unresolved": [], "ambiguities": [], "correction": None,
        }],
    ))
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "NET_INCOME": 25.0, "SALES_REVENUE": 100.0,
    })
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("извлеки чистую прибыль и посчитай рентабельность")

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert not errors, errors
    graph = next(event for event in events if event.get("type") == "plan_graph")
    net_income_locators = [
        node for node in graph["nodes"]
        if node["node_key"]["atom_name"] == "locate"
        and "concept=NET_INCOME" in node["node_key"]["args_fingerprint"]
    ]
    assert len(net_income_locators) == 1
    assert set(net_income_locators[0]["scenario_refs"]) == {"profit", "margin"}
    results = [event["result"] for event in events if event.get("type") == "typed_result"]
    assert {result["output_type"] for result in results} == {"ExtractedMetric", "CalculatedMetric"}


# ---------------------------------------------------------------------------
# П5: ad-hoc извлечение — описание придумала модель и оно доехало до экстрактора
def test_an_ad_hoc_concept_described_by_the_model_reaches_the_extractor(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    name = "Средневзвешенная ставка по кредитам и займам"
    set_intent(monkeypatch, _plan(
        intent_kind="extract_ad_hoc_metric", targets=[], unresolved=["средняя ставка по кредитам"],
        ad_hoc_concept={
            "name_ru": name, "name_en": "Weighted average interest rate on borrowings",
            "aliases_ru": ["средняя ставка по кредитам"], "aliases_en": ["weighted average interest rate"],
            "definition": "Средняя процентная ставка по долговым обязательствам.",
            "hint": "Примечание о займах, проценты годовых.", "value_kind": "ratio",
        },
    ))
    locate_exec, real_read_value = make_fake_locate_and_read_value_executors({name: 9.5})
    seen_extra: list[dict] = []

    def _spy(items, ctx):
        seen_extra.extend(dict(a.extra) for a in items)
        return real_read_value(items, ctx)

    patched_build_registry(monkeypatch, locate_exec, _spy)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи среднюю ставку по кредитам")

    metric = next(e["result"] for e in events if e.get("type") == "typed_result")
    assert metric["output_type"] == "ExtractedMetric" and metric["definition_origin"] == "ad_hoc"
    described = json.loads(seen_extra[0]["ad_hoc_concepts"])
    assert described[0]["aliases_ru"] == ["средняя ставка по кредитам"]
    assert described[0]["definition"].startswith("Средняя процентная ставка")
    manifest_text = turn.prompt
    assert "ad_hoc_definition" in manifest_text and "weighted average interest rate" in manifest_text, (
        "диспетчер знает, что именно искали, и может сказать это пользователю"
    )


# ---------------------------------------------------------------------------
# П4: расчёт без формулы от пользователя — формулу выдала модель
def test_a_formula_invented_by_the_model_with_registry_and_ad_hoc_components_is_calculated(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    set_intent(monkeypatch, _plan(
        intent_kind="calculate_ad_hoc_metric", targets=[], unresolved=["выручка на кв. метр торговой площади"],
        ad_hoc_formula={
            "name_ru": "Выручка на кв. метр торговой площади", "unit": "RUB",
            "expression": "REV / AREA",
            "components": [
                {"key": "REV", "registry_code": "SALES_REVENUE", "name_ru": "Выручка"},
                {"key": "AREA", "name_ru": "Площадь торговых залов тестовая",
                 "aliases_ru": ["торговая площадь"], "definition": "Площадь торговых залов на конец периода.",
                 "value_kind": "stock"},
            ],
            "assumptions": "площадь на конец периода",
        },
    ))
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "SALES_REVENUE": 1_000.0, "AREA": 250.0,
    })
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("посчитай выручку на квадратный метр торговой площади")

    assert not any(e.get("type") == "typed_error" for e in events), [e for e in events if e.get("type") == "typed_error"]
    result = next(e["result"] for e in events if e.get("type") == "typed_result")
    assert result["output_type"] == "CalculatedMetric" and result["definition_origin"] == "ad_hoc"
    assert result["derived_fact"]["value"] == 4.0
    assert len(result["derived_fact"]["inputs"]) == 2
    graph = next(e for e in events if e.get("type") == "plan_graph")
    atoms = {n["node_key"]["atom_name"] for n in graph["nodes"]}
    assert {"calc.formula", "locate", "read.value"} <= atoms, "компоненты извлекаются двухстадийно"
    assert "площадь на конец периода" in turn.prompt, "допущения формулы доступны диспетчеру"


# ---------------------------------------------------------------------------
# DynamicRun: цикл собирает и исполняет ходы из существующих атомов
def _dynamic_agent(monkeypatch, *responses: dict | str) -> list[dict]:
    """Подменяет модельную границу цикла; возвращает журнал вызовов."""
    calls: list[dict] = []

    async def _complete(system: str, prompt: str) -> str:
        calls.append({"system": system, "prompt": prompt})
        item = responses[min(len(calls), len(responses)) - 1]
        if callable(item):
            item = item(prompt)
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _complete)
    return calls


def _dynamic_execute(*steps: dict) -> dict:
    return {
        "objective": "получить данные", "reason": "они нужны для ответа", "action": "execute",
        "scenarios": [], "new_atoms": [], "steps": list(steps), "answer": "", "evidence_ids": [],
    }


def _dynamic_give_up(reason: str) -> dict:
    return {
        "objective": "остановиться", "reason": "пути исчерпаны", "action": "give_up",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": reason, "evidence_ids": [],
    }


def _finalize_from_prompt(prompt: str, output_type: str) -> dict:
    line = next(line for line in prompt.splitlines() if f"| {output_type} |" in line)
    evidence_id = line.split("|", 1)[0].strip()
    return {
        "objective": "ответить", "reason": "доказательство получено", "action": "finalize",
        "scenarios": [], "new_atoms": [], "steps": [], "answer": "Данные извлечены.",
        "evidence_ids": [evidence_id],
    }


def _document_with_the_quoted_page(monkeypatch) -> None:
    """Гейт finalize не пускает ответ поверх проваленной `verify.citation`: цитата со страницы 42
    должна реально быть в тексте документа."""
    monkeypatch.setattr(
        "finengine.store.blobs.document_text",
        lambda doc_id, **_kw: "===PAGE 42===\nНе менее 50% прибыли.\n",
    )


def test_a_goal_type_without_a_deterministic_root_is_composed_by_the_planner_agent(
    monkeypatch, make_fake_quote_executor,
):
    """`document_qa` не имеет детерминированного корня: сообщение проходит
    production ingress, DynamicRun и возвращается диспетчеру как манифест."""
    set_intent(monkeypatch, _plan(intent_kind="document_qa", targets=[]))
    agent_calls = _dynamic_agent(
        monkeypatch,
        _dynamic_execute({"atom": "read.quote", "concept": "дивидендная политика"}),
        lambda prompt: _finalize_from_prompt(prompt, "QuoteEvidence"),
    )
    patched_build_registry(
        monkeypatch,
        quote_executor=make_fake_quote_executor({"дивидендная политика": ("Не менее 50% прибыли.", 42)}),
    )
    turn = Turn()
    turn.arm(monkeypatch)
    _document_with_the_quoted_page(monkeypatch)

    events = turn.run("что сказано о дивидендной политике?")

    assert not any(e.get("type") == "typed_error" for e in events), {
        "errors": [e for e in events if e.get("type") == "typed_error"],
        "events": events,
        "planner_calls": agent_calls,
    }
    graph = next(e for e in events if e.get("type") == "plan_graph")
    assert graph["planner_version"].startswith("dynamic"), "UI отличает план агента от детерминированного"
    assert {n["node_key"]["atom_name"] for n in graph["nodes"]} >= {
        "read.quote", "verify.citation", "ingest.document",
    }
    assert any(e.get("type") == "tool_output" and e.get("ok") for e in events)
    assert len(agent_calls) == 2
    assert "не имеет производителя корня" in agent_calls[0]["prompt"], "агенту передана ошибка детерминированного планировщика"
    assert "document_qa" in agent_calls[0]["prompt"] and "дивидендной политике" in agent_calls[0]["prompt"]
    assert "- read.quote" in agent_calls[0]["prompt"], "и каталог атомов"
    assert "Не менее 50% прибыли" in agent_calls[1]["prompt"], "цикл видит результат графа перед finalize"
    assert RAW_TEXT_MARKER not in agent_calls[1]["prompt"]


def test_custom_analysis_goal_goes_to_the_planner_agent_not_to_the_deterministic_path(
    monkeypatch, make_fake_quote_executor,
):
    """`custom_analysis` вне `_GATED_KINDS`: цель идёт в DynamicRun, детерминированный
    planner не вызывается, а сырая просьба доходит до агента."""
    request = "проведи корреляционный анализ, результат анализа верни в excel"
    set_intent(monkeypatch, _plan(intent_kind="custom_analysis", targets=[], unresolved=[request]))
    agent_calls = _dynamic_agent(
        monkeypatch,
        _dynamic_execute({"atom": "read.quote", "concept": "дивидендная политика"}),
        lambda prompt: _finalize_from_prompt(prompt, "QuoteEvidence"),
    )
    patched_build_registry(
        monkeypatch,
        quote_executor=make_fake_quote_executor({"дивидендная политика": ("Не менее 50% прибыли.", 42)}),
    )
    turn = Turn()
    turn.arm(monkeypatch)
    _document_with_the_quoted_page(monkeypatch)

    events = turn.run("Извлеки показатели и " + request)

    graph = next(e for e in events if e.get("type") == "plan_graph")
    assert graph["planner_version"].startswith("dynamic")
    assert agent_calls, "цикл planner запущен"
    assert "custom_analysis" in agent_calls[0]["prompt"] and "корреляционный" in agent_calls[0]["prompt"]


def test_the_planner_answer_of_a_finalized_run_reaches_the_user(monkeypatch, make_fake_quote_executor):
    """После успешного гейта диспетчер не зовётся, а доказательство цикла (цитата, `dyn:*`) не
    типизированная карточка: без ответа планировщика пользователь увидел бы «см. карточки выше»
    при пустом экране."""
    set_intent(monkeypatch, _plan(intent_kind="document_qa", targets=[]))
    _dynamic_agent(
        monkeypatch,
        _dynamic_execute({"atom": "read.quote", "concept": "дивидендная политика"}),
        lambda prompt: {
            **_finalize_from_prompt(prompt, "QuoteEvidence"),
            "answer": "Дивиденды — не менее 50% прибыли.",
        },
    )
    patched_build_registry(
        monkeypatch,
        quote_executor=make_fake_quote_executor({"дивидендная политика": ("Не менее 50% прибыли.", 42)}),
    )
    turn = Turn()
    turn.arm(monkeypatch)
    _document_with_the_quoted_page(monkeypatch)

    events = turn.run("что сказано о дивидендной политике?")

    texts =[e["text"] for e in events if e.get("type") == "text_delta"]
    assert texts == ["Дивиденды — не менее 50% прибыли."], texts
    assert turn.dispatcher_calls == 0


def _routed_outcome(monkeypatch, manifest: dict):
    from finengine.skills import production_runtime as pr

    monkeypatch.setattr(
        pr, "execute_routed_goal",
        lambda **kw: pr.EvidenceGateOutcome(tool_name="financial_pipeline", manifest=manifest),
    )


@pytest.mark.parametrize(
    ("status", "answer"),
    [
        ("gave_up", "Для ответа нужен второй документ."),
        ("stopped", "Не удалось получить EBITDA; доступна только выручка."),
    ],
)
def test_non_error_dynamic_outcome_is_plain_history_for_the_next_router(monkeypatch, status, answer):
    recorder = set_intent_sequence(monkeypatch, [_plan(intent_kind="custom_analysis", targets=[]), _ANSWER])
    _routed_outcome(monkeypatch, {"dynamic_run": {"status": status, "answer": answer}})
    turn = Turn()
    turn.arm(monkeypatch)

    first = turn.run("сложный запрос")
    session_id = first[0]["session_id"]
    turn.run("продолжи", session_id=session_id)

    assert [event["text"] for event in first if event.get("type") == "text_delta"] == [answer]
    assert not any(event.get("type") == "typed_error" for event in first)
    next_prompt = recorder.calls[1]["prompt"]
    assert answer in next_prompt
    assert "[Типизированный отказ]" not in next_prompt


def test_a_deterministic_result_with_a_lost_request_says_what_was_not_done(monkeypatch):
    _routed_outcome(monkeypatch, {
        "result": {"status": "supported"}, "not_performed": ["проведи корреляционный анализ"],
    })
    set_intent(monkeypatch, _plan())
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку и проведи корреляционный анализ")

    (text,) = [e["text"] for e in events if e.get("type") == "text_delta"]
    assert text.startswith("Готово: проверенные результаты опубликованы")
    assert "Не выполнено: проведи корреляционный анализ" in text


def test_a_deterministic_result_without_a_lost_request_has_no_not_done_line(monkeypatch):
    _routed_outcome(monkeypatch, {"result": {"status": "supported"}, "not_performed": []})
    set_intent(monkeypatch, _plan())
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку")

    (text,) = [e["text"] for e in events if e.get("type") == "text_delta"]
    assert "Не выполнено" not in text


def test_a_refusal_gets_no_extra_not_done_line(monkeypatch):
    _routed_outcome(monkeypatch, {
        "error": {"code": "x", "message": "своё сообщение отказа"}, "not_performed": ["что-то"],
    })
    set_intent(monkeypatch, _plan())
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку")

    assert not any(e.get("type") == "text_delta" and "Не выполнено" in e["text"] for e in events)


def test_an_infeasible_deterministic_plan_falls_back_to_the_planner_agent(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    from finengine.skills.case_runtime import CaseRuntime, PlanInfeasible

    real_plan = CaseRuntime.plan
    seen: list[str] = []

    def _infeasible_once(self, **kw):
        if not seen:
            seen.append("x")
            raise PlanInfeasible("Case delta-plan is not feasible: недостижимые концепты: SALES_REVENUE")
        return real_plan(self, **kw)

    monkeypatch.setattr(CaseRuntime, "plan", _infeasible_once)
    set_intent(monkeypatch, _plan())
    agent_calls = _dynamic_agent(
        monkeypatch,
        _dynamic_execute({"atom": "read.value", "concept": "SALES_REVENUE"}),
        lambda prompt: _finalize_from_prompt(prompt, "ExtractedMetric"),
    )
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 321.0})
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("извлеки выручку")

    assert not any(e.get("type") == "typed_error" for e in events)
    graph = next(e for e in events if e.get("type") == "plan_graph")
    assert graph["planner_version"].startswith("dynamic")
    assert "недостижимые концепты: SALES_REVENUE" in agent_calls[0]["prompt"]
    typed = [e["result"] for e in events if e.get("type") == "typed_result"]
    assert typed and typed[0]["output_type"] == "ExtractedMetric", "результат — та же типизированная карточка"


def test_a_planner_agent_that_cannot_plan_gives_one_answer_and_no_document_to_the_dispatcher(monkeypatch):
    set_intent(monkeypatch, _plan(intent_kind="validate_report", targets=[]))
    _dynamic_agent(monkeypatch, _dynamic_give_up("для сверки нужен второй документ"))
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("сверь этот отчёт с прошлогодним")

    texts = [e["text"] for e in events if e.get("type") == "text_delta"]
    assert texts == ["для сверки нужен второй документ"]
    assert not any(e.get("type") == "typed_error" for e in events)
    assert turn.dispatcher_calls == 0


def test_an_invalid_agent_plan_is_never_executed(monkeypatch):
    set_intent(monkeypatch, _plan(intent_kind="compare_metrics", targets=[]))
    calls = _dynamic_agent(
        monkeypatch,
        _dynamic_execute({"atom": "verify.bounds", "concept": "x"}),
        _dynamic_give_up("ход отклонён"),
    )
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("сравни выручку с прошлым годом")

    assert any(e.get("type") == "text_delta" and e["text"] == "ход отклонён" for e in events)
    assert not any(e.get("type") == "typed_error" for e in events)
    assert "plan_graph" not in _types(events) and "node_run" not in _types(events), "невалидный план не исполняется"
    assert len(calls) == 2, "лимит попыток"
    assert "нельзя планировать вручную" in calls[1]["prompt"], "агенту вернули причину"
    assert turn.dispatcher_calls == 0


# ---------------------------------------------------------------------------
# Живая проверка (M3, dev): показателя в отчёте просто нет — а пользователь
# получал `canonical_pipeline_incomplete`, из которого ничего не понять.
def test_an_ad_hoc_metric_missing_from_the_report_says_what_was_searched(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    set_intent(monkeypatch, _plan(
        intent_kind="extract_ad_hoc_metric", targets=[], unresolved=["среднесписочная численность"],
        ad_hoc_concept={
            "name_ru": "Среднесписочная численность работников", "aliases_ru": ["среднесписочная численность", "штат"],
            "aliases_en": ["average headcount"], "value_kind": "stock",
        },
    ))
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({})  # в отчёте этого нет
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи среднесписочную численность")

    errors = [e["error"] for e in events if e.get("type") == "typed_error"]
    assert [e["code"] for e in errors] == ["metric_not_found"]
    message = errors[0]["message"]
    assert "Среднесписочная численность работников" in message and "штат" in message, (
        "пользователь видит, ЧТО именно искали и по каким названиям"
    )
    assert errors[0]["retryable"] is False
    assert turn.dispatcher_calls == 0


def test_a_registered_metric_missing_from_the_report_names_the_metric(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    set_intent(monkeypatch, _plan(targets=[{"concept": "SALES_REVENUE"}]))
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({})
    patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку")

    errors = [e["error"] for e in events if e.get("type") == "typed_error"]
    assert [e["code"] for e in errors] == ["metric_not_found"]
    assert "SALES_REVENUE" in errors[0]["message"]


def test_a_failed_node_goes_to_the_cycle_with_its_cause_not_to_a_generic_refusal(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    """Часть 14 (Р14.1) переписала прежнее ожидание «упавший узел назван в отказе»: узел упал при исполнении —
    задачу подхватывает цикл, и причина (узел и его текст) идёт планировщику, а пользователю — один ответ.
    Отказ с названием узла остаётся там, где цикл невозможен (вложенный сценарий): `test_execution_failure_entry.py`."""
    set_intent(monkeypatch, _plan())
    locate_exec, _ = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})

    def _boom(items, ctx):  # noqa: ARG001
        raise RuntimeError("модель чтения недоступна")

    patched_build_registry(monkeypatch, locate_exec, _boom)
    prompts: list[str] = []

    async def _planner(system, prompt):  # noqa: ARG001
        prompts.append(prompt)
        return json.dumps({
            "objective": "остановиться", "reason": "чтение недоступно", "action": "give_up", "scenarios": [],
            "new_atoms": [], "steps": [], "answer": "Чтение отчёта сейчас недоступно.", "evidence_ids": [],
        }, ensure_ascii=False)

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _planner)
    turn = Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи выручку")

    assert not [e for e in events if e.get("type") == "typed_error"], "ошибка первой попытки в чат не уходит"
    assert prompts and "read.value" in prompts[0] and "модель чтения недоступна" in prompts[0]
    assert [e["text"] for e in events if e.get("type") == "text_delta"] == ["Чтение отчёта сейчас недоступно."]
