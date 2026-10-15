"""Срез 29 → Срез 31: сквозные тесты входа чата через настоящий
`FinengineAgent.query_stream`, не через станционный скрипт напрямую.

Ingress v2 (docs/build-plan/31-vozvrat-k-zamyslu-vladeltsa-ingress-v2.md):
КАЖДОЕ сообщение сначала попадает в модельный атом разбора, и модель решает —
передать запрос планировщику или ответить самой. Прежней механики Срезов
29/29a/30 (регэксп-переопределения решения модели, ретраи парсера, period
prepass, уточнение периода, состояние уточнений в `Session`) здесь больше нет —
ровно поэтому соответствующих тестов тоже нет. Новые сценарии (маршрут, все
периоды, роль, ad-hoc) — в `test_ingress_v2.py`.

Модельная граница подменяется в ДВУХ местах, как это делает production:
`intent.parse`'s собственный вызов (`intent._default_complete`) и `locate`/
`read.value`. Диспетчерская модель (то, что видит `agent.llm.stream_events`) —
третья отдельная подмена: тест смотрит, ЧТО она получила в промпте, а не
пытается угадать её решение. Общие опоры — `ingress_support.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.contracts import GoalIR, TargetNode
from finengine.orchestrator import agent as ag

from ingress_support import (  # noqa: F401 — `gate_env`: autouse-фикстура окружения
    DOC,
    OWNER,
    FakeMeta as _FakeMeta,
    FakeStore as _FakeStore,
    Turn as _Turn,
    gate_env as _gate_env,
    patched_build_registry as _patched_build_registry,
    set_intent as _intent,
    set_intent_sequence as _intent_sequence,
)


def test_a_numeric_question_never_reaches_the_model_without_a_canonical_result(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    """§1.1 живого сценария: «какая выручка?» больше не может уйти в ответ
    прямо по тексту — canonical intent.parse -> PlanGraph -> GraphExecutor
    отрабатывает КОДОМ раньше, чем модель получает ход."""
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)

    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("какая выручка за 2024 год?")

    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    tool_outputs = [e for e in events if e.get("type") == "tool_output"]
    assert any(e.get("name") == "financial_pipeline" for e in tool_calls), (
        "числовой вопрос про документ должен пройти evidence-gate — tool_call не появился")
    assert any(e.get("name") == "financial_pipeline" and e.get("ok") for e in tool_outputs), (
        "evidence-gate не выдал успешный tool_output")
    assert turn.prompt == "", (
        "после успешного evidence-gate второй модельный путь вообще не должен запускаться")
    assert any(
        e.get("type") == "text_delta" and "проверенные результаты" in e.get("text", "")
        for e in events
    )

    # Этап 7: plan_graph — ровно один раз, ДО первого node_run, и число узлов
    # совпадает с числом уникальных node_key среди последующих node_run.
    plan_graphs = [e for e in events if e.get("type") == "plan_graph"]
    node_runs = [e for e in events if e.get("type") == "node_run"]
    assert len(plan_graphs) == 1, "topология обязана прийти РОВНО один раз за исполнение"
    assert node_runs, "фейковые locate/read.value всё равно проходят через настоящий NodeRun — ждём node_run"
    assert events.index(plan_graphs[0]) < events.index(node_runs[0]), (
        "plan_graph обязан прийти ДО первого node_run — иначе владелец не увидит построенный "
        "граф до начала исполнения"
    )
    graph_node_keys = {json.dumps(n["node_key"], sort_keys=True) for n in plan_graphs[0]["nodes"]}
    run_node_keys = {json.dumps(e["node_run"]["node_key"], sort_keys=True) for e in node_runs}
    assert run_node_keys <= graph_node_keys, (
        "у каждого node_run обязан быть соответствующий узел в объявленной топологии plan_graph"
    )

    typed_results = [e for e in events if e.get("type") == "typed_result"]
    assert len(typed_results) == 1 and typed_results[0]["result"]["output_type"] == "ExtractedMetric", (
        "гейт обязан дать ровно один настоящий typed_result — одну структурную карточку, "
        "не дублировать уже эмитнутый CaseRuntime результат"
    )


def test_successful_gate_skips_every_second_numeric_model_path(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    calls: list[dict] = []

    async def _dispatcher(**kwargs):
        calls.append(kwargs)
        yield {"type": "done", "usage": {}}

    monkeypatch.setattr(ag, "_build_context", lambda *_a, **_kw: ("сырой текст с числами", []))
    monkeypatch.setattr(ag.llm, "stream_events", _dispatcher)
    agent = ag.FinengineAgent()

    async def _go():
        return [event async for event in agent.query_stream(
            "какая выручка за 2024 год?", doc_ids=[DOC], auth_user_id=OWNER,
            owner_id=OWNER, project_id="p-lukoil", gateway_session_key="live-gateway-key",
        )]

    import asyncio
    events = asyncio.run(_go())

    assert any(event.get("type") == "tool_output" and event.get("name") == "financial_pipeline" for event in events)
    assert calls == []
    assert any(
        event.get("type") == "text_delta" and "проверенные результаты" in event.get("text", "")
        for event in events
    )


def test_a_question_not_about_document_data_is_unaffected(monkeypatch):
    """Регрессия: вопрос не про число из отчёта — модель отвечает сама, ход идёт
    диспетчеру как раньше, без конвейера."""
    _intent(monkeypatch, {"route": "answer", "intent_kind": None, "targets": []})
    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("на каком языке этот отчёт?")

    assert not any(e.get("type") == "tool_call" and e.get("name") == "financial_pipeline" for e in events), (
        "модель выбрала ответ по контексту — гейт не должен был сработать")
    assert turn.dispatcher_calls == 1


def test_multiple_named_metrics_collapse_into_one_scoped_form_not_manual_lookup(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    """§1.4/Этап 3: «доходы и расходы» — ОДИН связный FormOutput, ограниченный
    именно запрошенными концептами, не диспетчер, вручную перечисляющий коды
    реестра, и не многократный extract.

    `intent.parse` детерминированно проверяет КАЖДЫЙ заявленный моделью
    концепт против реального `candidate_concepts(raw_query)` (Срез 4,
    `_finalize_goal`) — подмена одной модели здесь недостаточна, запрос
    обязан реально находить оба enum'а обычным поиском по реестру, поэтому
    формулировка и коды ниже — настоящие (`COST_OF_SALES` не существует в
    реестре, реальный enum — `COSTS_EXPENSES`)."""
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric",
        "targets": [{"concept": "SALES_REVENUE"}, {"concept": "COSTS_EXPENSES"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "SALES_REVENUE": 1_000.0, "COSTS_EXPENSES": 600.0,
    })
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)

    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("выручка и себестоимость продаж за 2024 год")

    tool_outputs = [e for e in events if e.get("type") == "tool_output" and e.get("name") == "financial_pipeline"]
    assert len(tool_outputs) == 1, "многопоказательный запрос обязан дать ОДИН результат гейта, не несколько"
    assert turn.prompt == "", "готовый FormOutput не должен запускать второй модельный вызов"


def test_ambiguous_financial_query_is_typed_clarification_not_dispatcher_bypass(monkeypatch):
    """Регрессия живого UI 24.09: «Извлеки прибыль» не должно разрешать
    свободной модели обещать несуществующий запуск и затем звать legacy
    station. Gate не угадывает вид прибыли, но и не молчит: он fail-closed
    просит типизированное уточнение и сохраняет контекст следующего хода."""
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [],
        "ambiguities": [{"term": "прибыль", "candidates": ["NET_INCOME", "EBITDA_MARGIN"], "reason": "?"}],
    })
    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("Извлеки прибыль")

    assert any(e.get("type") == "tool_call" and e.get("name") == "financial_pipeline" for e in events)
    assert any(
        e.get("type") == "typed_error"
        and e.get("error", {}).get("code") == "goal_needs_clarification"
        for e in events
    )
    assert not turn.prompt, "неоднозначный финансовый запрос не должен попасть свободному диспетчеру"
    assert not any(e.get("type") == "text_delta" for e in events)


def test_recognized_unwired_goal_type_goes_to_the_planner_agent_after_a_single_parse(monkeypatch):
    """D1 (Срез 30) → Срез 31: УВЕРЕННО распознанный, но ещё не подключённый GoalType
    (`extract_structure`) не превращается кодом в метрический запрос (никаких
    повторных попыток разбора и «уточните показатели и период») и не уходит
    свободному диспетчеру с сырым документом: его берёт агент-планировщик. Здесь агент
    честно отказывается — это типизированный `plan_not_composable`."""
    recorder = _intent_sequence(monkeypatch, [{
        "intent_kind": "extract_structure", "targets": [],
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [],
    }])

    async def _agent(system: str, prompt: str) -> str:
        return json.dumps({
            "objective": "остановиться", "reason": "пути исчерпаны", "action": "give_up",
            "scenarios": [], "new_atoms": [], "steps": [],
            "answer": "структуры долга нет среди атомов", "evidence_ids": [],
        }, ensure_ascii=False)

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _agent)
    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("извлеки структуру долга")

    assert len(recorder.calls) == 1, "один разбор на сообщение — без скрытых повторных попыток"
    assert not any(e.get("type") == "typed_error" for e in events)
    assert [e["text"] for e in events if e.get("type") == "text_delta"] == ["структуры долга нет среди атомов"]
    assert not turn.prompt, "диспетчер не получает сырой документ"


def test_a_gate_failure_log_names_session_case_document_and_intent_kind(monkeypatch, caplog):
    """Срез 30, этап 5 наблюдаемости: сбой ПОСЛЕ разбора запроса должен быть
    опознаваем по логу контейнера (Dozzle) без чтения кода — какой чат, какой
    Case, какой документ и какой GoalType упали."""
    def _boom(**_kw):
        raise RuntimeError("хранилище Case недоступно")

    monkeypatch.setattr("finengine.skills.production_runtime.prepare_production_context", _boom)
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    turn = _Turn()
    turn.arm(monkeypatch)
    with caplog.at_level("ERROR", logger="finengine.orchestrator.agent"):
        events = turn.run("какая выручка за 2024 год?")

    assert any(e.get("type") == "typed_error" and e["error"]["code"] == "evidence_gate_failed" for e in events)
    message = next(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "case=p-lukoil" in message
    assert f"doc={DOC}" in message
    assert "intent_kind=extract_registered_metric" in message
    assert "session=" in message
    assert any(r.exc_info for r in caplog.records if r.levelname == "ERROR")


def test_a_gate_exception_is_logged_at_error_not_swallowed(monkeypatch, caplog):
    """Mandatory evidence boundary fails closed: an infrastructure error is
    logged with traceback and becomes a typed refusal.  The dispatcher must
    not receive the document and manufacture a numeric answer."""
    def _boom():
        raise RuntimeError("хранилище документов недоступно на этом стенде")

    monkeypatch.setattr(ag, "get_store", _boom)
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    turn = _Turn()
    turn.arm(monkeypatch)
    with caplog.at_level("ERROR", logger="finengine.orchestrator.agent"):
        events = turn.run("какая выручка?")

    assert any(
        e.get("type") == "typed_error"
        and e.get("error", {}).get("code") == "evidence_gate_failed"
        for e in events
    )
    assert any(
        e.get("type") == "tool_output" and e.get("name") == "financial_pipeline" and not e.get("ok")
        for e in events
    )
    assert not turn.prompt, "при падении обязательного гейта свободный диспетчер не вызывается"
    assert not any(e.get("type") == "text_delta" for e in events)
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "исключение внутри гейта обязано попасть в лог на уровне ERROR"
    assert any(r.exc_info for r in error_records), (
        "лог обязан нести exc_info=True — иначе стек трассировки теряется"
    )


def test_plan_graph_emission_failure_is_logged_not_silently_dropped(
    monkeypatch, caplog, make_fake_locate_and_read_value_executors, severstal_text,
):
    """Этап 8, п.3: сбой сериализации/эмиссии plan_graph — то же правило, что
    у самого гейта — обязан быть виден в логах, а исполнение (сам показатель)
    не обязано провалиться из-за отказа наблюдаемости."""
    from finengine.skills.production_runtime import execute_routed_goal

    goal = GoalIR(
        raw_query="какая выручка за 2024 год?", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")], period_ref="p-2024-fy",
    )
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)

    def _flaky_sink(event: dict) -> None:
        if event.get("type") == "plan_graph":
            raise RuntimeError("сериализация графа упала")

    with caplog.at_level("ERROR", logger="finengine.skills.case_runtime"):
        outcome = execute_routed_goal(
            owner_id=OWNER, case_id="case-plan-graph-failure", document_id=DOC,
            document_text=severstal_text, goal=goal, event_sink=_flaky_sink,
        )

    assert outcome is not None and "SALES_REVENUE" in json.dumps(outcome.manifest, ensure_ascii=False), (
        "отказ наблюдаемости (plan_graph) не должен ронять сам расчёт показателя"
    )
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "сбой эмиссии plan_graph обязан попасть в лог на уровне ERROR"
    assert any(r.exc_info for r in error_records)


def test_multiple_documents_without_an_active_one_leave_the_gate_silent(monkeypatch, severstal_text):
    """Один документ или явный активный — единственный поддерживаемый адрес
    гейта в этой сессии; несколько без активного — свободный ход, не крашится."""
    monkeypatch.setattr(ag, "get_store", lambda: _FakeStore({
        DOC: _FakeMeta(OWNER), "doc-second": _FakeMeta(OWNER),
    }))
    monkeypatch.setattr("finengine.store.blobs.document_text", lambda doc_id, **_kw: (
        severstal_text if doc_id in (DOC, "doc-second") else None
    ))
    _intent(monkeypatch, {
        "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run("какая выручка?", doc_ids=(DOC, "doc-second"))

    assert not any(e.get("type") == "tool_call" and e.get("name") == "financial_pipeline" for e in events), (
        "несколько документов без active_doc_id — неоднозначный адрес, гейт должен молчать"
    )


# ---------------------------------------------------------------------------
# Явно названный период остаётся фильтром (Срез 31, П6 сохраняет его): прямой
# вызов `execute_routed_goal` с готовой целью — не нужен полный `query_stream`,
# поведение детерминировано и не зависит от диспетчерской модели.

# Добавка к `severstal_text` (не выдуманный документ с нуля — реальный excerpt
# classify_frame() уже надёжно узнаёт как `ifrs_nf_ru`; добавка несёт только
# полугодовые периоды, которые проверяет конкретный тест).
_EXTRA_HALF_YEAR = (
    "\n\nПромежуточная сокращенная консолидированная финансовая отчетность за "
    "шесть месяцев, закончившихся 30 июня 2026 года и 30 июня 2025 года.\n"
    "Выручка 1 000 900"
)


@pytest.mark.parametrize("requested_period", ["6M 2025", "1H 2025"])
def test_requested_period_is_selected_from_comparative_extractor_results(
    monkeypatch, make_fake_locate_and_read_value_executors, severstal_text, requested_period,
):
    """read.value may return both comparative columns.  Its graph adapter
    keeps only the period requested by GoalIR before normalization starts.
    """
    from finengine.contracts import ScalarFact
    from finengine.skills.production_runtime import execute_routed_goal

    goal = GoalIR(
        raw_query="извлеки выручку за первое полугодие 2025", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="SALES_REVENUE", output_kind="scalar")], period_ref=requested_period,
    )
    locate_exec, _ = make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})

    def _comparatives(items, ctx):  # noqa: ARG001
        out = {}
        for args in items:
            out[args.concept] = tuple(
                ScalarFact(
                    produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                    source_ref=f"comparative:{args.concept}:{period}", concept=args.concept,
                    period_ref=period, value=value, unit="RUB",
                )
                for period, value in (("6M 2025", 100.0), ("6M 2026", 200.0))
            )
        return out

    _patched_build_registry(monkeypatch, locate_exec, _comparatives)
    events: list[dict] = []
    outcome = execute_routed_goal(
        owner_id=OWNER, case_id=f"case-period-comparative-choice-{requested_period}", document_id=DOC,
        document_text=severstal_text + _EXTRA_HALF_YEAR, goal=goal, event_sink=events.append,
    )

    assert outcome is not None and "error" not in outcome.manifest
    assert outcome.manifest["result"]["period_ref"] == "6M 2025"
    assert outcome.manifest["period_scope"] == "selected"
    typed_results = [event for event in events if event.get("type") == "typed_result"]
    assert len(typed_results) == 1
    assert typed_results[0]["result"]["period_ref"] == "6M 2025"


# ---------------------------------------------------------------------------
# Оставшаяся матрица Среза 27: каждый сценарий обязан пройти именно через
# боевой FinengineAgent.query_stream.  Станционные тесты ниже этого ingress
# не доказывают: они начинают уже после решения «вызвать конвейер».


def _assert_query_stream_terminal(events: list[dict], output_type: str) -> tuple[dict, dict]:
    plan_graphs = [event for event in events if event.get("type") == "plan_graph"]
    node_runs = [event for event in events if event.get("type") == "node_run"]
    terminals = [
        event for event in events
        if event.get("type") == "typed_result"
        and event.get("result", {}).get("output_type") == output_type
    ]
    assert len(plan_graphs) == 1
    assert node_runs
    assert len(terminals) == 1
    graph = plan_graphs[0]
    assert all(event.get("plan_graph_hash") == graph["graph_hash"] for event in node_runs)
    assert events.index(graph) < events.index(node_runs[0])
    assert not any(event.get("type") == "typed_error" for event in events)
    # Без этого «в prompt нет текста документа» было бы пустой проверкой:
    # диспетчер обязан реально получить канонический результат как tool_output.
    assert any(
        event.get("type") == "tool_output" and event.get("name") == "financial_pipeline"
        and event.get("ok") for event in events
    )
    return graph, terminals[0]["result"]


def test_extract_ad_hoc_metric_reaches_typed_root_through_query_stream(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    term = "площадь торговых залов"
    _intent(monkeypatch, {
        "intent_kind": "extract_ad_hoc_metric", "targets": [],
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": [term], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({term: 321.0})
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("покажи площадь торговых залов за 2024 год"), "ExtractedMetric",
    )

    assert graph["root"]["atom_name"] == "verify.bounds"
    assert result["definition_origin"] == "ad_hoc"
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def test_full_analysis_gets_a_longer_execution_budget_than_a_single_metric(monkeypatch):
    """Срез 30, real-model smoke: граф FULL_ANALYSIS на реальном отчёте — 627
    узлов, на свободной системе он исполнился за 279 с, а под нагрузкой ушёл за
    бюджет GraphExecutor по умолчанию (300 с) — `TimeoutError`, отчёта нет.
    Бюджет времени обязан быть соразмерен графу; для одного показателя
    остаётся прежний."""
    budgets: dict[str, object] = {}

    def _capture(kind: str):
        def _prepare(**kwargs):
            budgets[kind] = kwargs.get("budget")
            raise RuntimeError("остановка после захвата бюджета")
        return _prepare

    for kind, payload in (
        ("full_analysis", {"intent_kind": "full_analysis", "targets": []}),
        ("extract", {"intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}]}),
    ):
        _intent(monkeypatch, {
            **payload, "entity_ref": None, "period_ref": "p-2024-fy", "role": "other",
            "unresolved": [], "ambiguities": [],
        })
        monkeypatch.setattr("finengine.skills.production_runtime.prepare_production_context", _capture(kind))
        turn = _Turn()
        turn.arm(monkeypatch)
        turn.run("сделай анализ за 2024 год" if kind == "full_analysis" else "какая выручка за 2024 год?")

    assert budgets["full_analysis"].max_wall_seconds >= 1800
    assert budgets["extract"] is None, "для одного показателя бюджет остаётся прежним (по умолчанию)"


def test_registered_kind_with_only_an_unresolved_phrase_is_a_typed_clarification_not_a_bypass(monkeypatch):
    """Срез 30: real-model smoke дал для «покажи <термин вне реестра>» вид
    `extract_registered_metric` + пустые targets + фраза в `unresolved` — раньше
    gate считал это неуверенным резолвом, молчал, и ход уходил свободному
    диспетчеру с сырым документом (881 с, 8 чтений источника, веб-поиск).
    Вид не переписывается кодом (краевой случай 8: решение registered/ad-hoc
    принадлежит intent.parse), но и обходом это быть не может: typed-уточнение
    с названием непонятого показателя. Срез 31: повторных попыток разбора нет —
    решение принимает модель, один разбор на сообщение."""
    recorder = _intent_sequence(monkeypatch, [{
        "intent_kind": "extract_registered_metric", "targets": [],
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": ["средневзвешенное количество акций"], "ambiguities": [],
    }])
    turn = _Turn()
    turn.arm(monkeypatch)

    events = turn.run("покажи средневзвешенное количество акций за 2024 год")

    errors = [e["error"] for e in events if e.get("type") == "typed_error"]
    assert [e["code"] for e in errors] == ["goal_needs_clarification"]
    assert "средневзвешенное количество акций" in errors[0]["message"]
    assert len(recorder.calls) == 1, "один разбор на сообщение — без скрытых повторных попыток"
    assert not turn.prompt, "диспетчер не получает ни prompt, ни текст документа"
    assert not any(e.get("type") == "text_delta" for e in events)


def test_calculate_ad_hoc_without_a_trusted_formula_enters_the_dynamic_run_not_a_gate_failure(
    monkeypatch, caplog,
):
    """Срез 30/31: `root_from_goal` для `calculate_ad_hoc_metric` без доверенной
    формулы поднимает ValueError — пользователь не должен получать «конвейер
    временно недоступен» и ERROR-трейс в Dozzle за штатную ситуацию «модель не
    смогла составить проверяемую формулу». Часть 13 (Р13.1): это сбой планирования, а не
    неоднозначность, — вместо отказа `goal_not_executable` сообщение получает цикл
    `DynamicRun`, и причина доходит до планировщика как ошибка детерминированного пути.
    Срез 31: формулу выдаёт МОДЕЛЬ; просить её у пользователя больше не нужно, поэтому и
    повторных попыток разбора нет."""
    import logging

    recorder = _intent_sequence(monkeypatch, [{
        "intent_kind": "calculate_ad_hoc_metric", "targets": [],
        "entity_ref": None, "period_ref": "p-2024-fy", "role": None,
        "unresolved": ["оборачиваемость запасов"], "ambiguities": [],
    }])
    planner_prompts: list[str] = []

    async def _planner(system: str, prompt: str) -> str:
        planner_prompts.append(prompt)
        return json.dumps({
            "objective": "остановиться", "reason": "нет данных", "action": "give_up", "scenarios": [],
            "new_atoms": [], "steps": [], "answer": "Оборачиваемость запасов посчитать не удалось.",
            "evidence_ids": [],
        }, ensure_ascii=False)

    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _planner)
    turn = _Turn()
    turn.arm(monkeypatch)

    with caplog.at_level(logging.WARNING):
        events = turn.run("посчитай оборачиваемость запасов за 2024 год")

    assert not [e for e in events if e.get("type") == "typed_error"]
    assert any("Оборачиваемость запасов посчитать не удалось." in e.get("text", "") for e in events)
    assert len(recorder.calls) == 1, "один разбор на сообщение"
    assert len(planner_prompts) == 1 and "проверяемую формулу" in planner_prompts[0]
    assert not turn.prompt
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("deterministic planning failed" in r.getMessage() for r in caplog.records)


def test_calculate_registered_metric_reaches_typed_root_through_query_stream(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    _intent(monkeypatch, {
        "intent_kind": "calculate_registered_metric", "targets": [{"concept": "EBITDA"}],
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "OPERATING_PROFIT": 400.0, "DEPRECIATION_AMORTIZATION": 60.0,
    })
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("рассчитай EBITDA за 2024 год"), "CalculatedMetric",
    )

    assert any(node["node_key"]["atom_name"] == "calc.formula" for node in graph["nodes"])
    assert result["concept"] == "EBITDA"
    assert result["derived_fact"]["value"] == 460.0
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def test_calculate_ad_hoc_metric_reaches_typed_root_through_query_stream(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    term = "выручка минус чистая прибыль"
    _intent(monkeypatch, {
        "intent_kind": "calculate_ad_hoc_metric", "targets": [],
        "ad_hoc_formula": {
            "expression": "SALES_REVENUE - NET_INCOME",
            "dependencies": ["SALES_REVENUE", "NET_INCOME"],
        },
        "entity_ref": "severstal", "period_ref": "p-2024-fy", "role": None,
        "unresolved": [term], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "SALES_REVENUE": 1_000.0, "NET_INCOME": 120.0,
    })
    _patched_build_registry(monkeypatch, locate_exec, read_value_exec)
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("посчитай выручку минус чистая прибыль за 2024 год"), "CalculatedMetric",
    )

    assert any(node["node_key"]["atom_name"] == "calc.formula" for node in graph["nodes"])
    assert result["definition_origin"] == "ad_hoc"
    assert result["derived_fact"]["value"] == 880.0
    assert len(result["derived_fact"]["inputs"]) == 2
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def test_full_analysis_reaches_report_root_through_query_stream(
    monkeypatch, make_fake_locate_and_read_value_executors,
    make_fake_structure_executor, make_fake_entities_executor,
):
    from tests.integration.test_intent_to_graph import (
        _fake_explanation_batch,
        _fake_external_executor,
        _fake_judgment_batch,
        _fake_narrative_executor,
    )

    _intent(monkeypatch, {
        "intent_kind": "full_analysis", "targets": [], "entity_ref": "severstal",
        "period_ref": "p-2024-fy", "role": "other", "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "SALES_REVENUE": 1_000.0, "NET_INCOME": 120.0,
        "INTEREST_PAID": 9.0, "INTEREST_RECEIVED": 2.0,
    })
    _patched_build_registry(
        monkeypatch, locate_exec, read_value_exec,
        structure_executor=make_fake_structure_executor({}),
        entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch,
        explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor,
        narrative_executor=_fake_narrative_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("сделай полный финансовый анализ за 2024 год"), "ReportOutput",
    )

    assert graph["root"]["atom_name"] == "verify.citation"
    assert any(node["node_key"]["atom_name"] == "out.report" for node in graph["nodes"])
    assert result["output_type"] == "ReportOutput"
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def test_explain_change_over_all_periods_reaches_scoped_narrative_through_query_stream(
    monkeypatch, make_fake_locate_and_read_value_executors,
    make_fake_structure_executor, make_fake_entities_executor,
):
    """Живой отказ 24.09.2026: «Объясни разницу финансовых вложений между всеми
    периодами» дважды заканчивался `goal_needs_clarification`, хотя запрос
    целиком разобран моделью как `explain_change`. Причина — `explain_change`
    не входил в `_GATED_KINDS`, и gate принимал его за нераспознанный
    финансовый запрос. Запрос не называет ни компанию (активный PDF уже
    выбран), ни один период («все периоды» — это вся ось документа): gate не
    имеет права ни требовать их, ни выдумывать."""
    from tests.integration.test_intent_to_graph import (
        _fake_explanation_batch,
        _fake_external_executor,
        _fake_judgment_batch,
        _fake_narrative_executor,
    )

    # Показатели финансовых вложений существуют только в профиле `rsbu_nf_ru`
    # (в `ifrs_*` для них нет производителей, и изолированная ветка молча
    # отбрасывается) — как и отчёт «Бухгалтерская отчётность на 30.06.2026» из
    # жалобы. Поэтому активный документ здесь — РСБУ-выдержка из golden-набора
    # классификации, а не IFRS-фикстура `severstal_text` по умолчанию.
    rsbu_cases = json.loads(
        (Path(__file__).parents[1] / "golden" / "classification" / "cases.json").read_text(encoding="utf-8")
    )
    rsbu_excerpt = next(case["excerpt"] for case in rsbu_cases if case["profile_id"] == "rsbu_nf_ru")
    monkeypatch.setattr(
        "finengine.store.blobs.document_text",
        lambda doc_id, **_kw: rsbu_excerpt if doc_id == DOC else None,
    )
    concepts = ["LONG_TERM_FINANCIAL_INVESTMENTS", "SHORT_TERM_FINANCIAL_INVESTMENTS"]
    _intent(monkeypatch, {
        "intent_kind": "explain_change", "targets": [],
        "analysis_concepts": concepts, "period_scope": "all_available",
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [],
    })
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({
        "LONG_TERM_FINANCIAL_INVESTMENTS": 120.0,
        "SHORT_TERM_FINANCIAL_INVESTMENTS": 30.0,
    })
    _patched_build_registry(
        monkeypatch, locate_exec, read_value_exec,
        structure_executor=make_fake_structure_executor({}),
        entities_executor=make_fake_entities_executor([]),
        judgment_executor=_fake_judgment_batch,
        explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor,
        narrative_executor=_fake_narrative_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("Объясни разницу финансовых вложений между всеми периодами"),
        "NarrativeOutput",
    )

    atoms = [node["node_key"]["atom_name"] for node in graph["nodes"]]
    assert {"ref.prior", "assess.judgment", "explain.internal", "out.narrative"} <= set(atoms)
    # Scope ограничен названными показателями: не полный ролевой набор и не
    # ветка внешнего контекста, которую пользователь не просил.
    assert "explain.external" not in atoms
    assert result["output_type"] == "NarrativeOutput"
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def test_explain_change_without_named_metrics_is_a_typed_clarification_not_a_bypass(monkeypatch):
    """Модель распознала «объясни разницу», но не назвала ни одного показателя
    (`analysis_concepts` пуст): исполнимого корня нет. Это честное typed-
    уточнение, а не передача сырого документа свободному диспетчеру."""
    _intent(monkeypatch, {
        "intent_kind": "explain_change", "targets": [],
        "analysis_concepts": [], "period_scope": "all_available",
        "entity_ref": None, "period_ref": None, "role": None,
        "unresolved": [], "ambiguities": [],
    })
    turn = _Turn()
    turn.arm(monkeypatch)

    events = turn.run("Объясни разницу финансовых вложений между всеми периодами")

    errors = [event for event in events if event.get("type") == "typed_error"]
    assert [event["error"]["code"] for event in errors] == ["goal_needs_clarification"]
    assert not any(event.get("type") == "plan_graph" for event in events)
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt


def _arm_monitor_env(
    monkeypatch, make_locate, make_structure, make_entities, *, found: bool = True,
):
    """Подставные модельные границы для MONITOR_CHANGES (Срез 32): двухпериодный
    `read.value`, фейки суждений/объяснений/текста и ЖУРНАЛ вызовов извлечения — по
    нему видно, какие показатели уходили в `locate`/`read.value` при каждом запросе."""
    from tests.integration.test_graph_analysis_narrative import (
        _fake_explanation_batch,
        _fake_external_executor,
        _fake_judgment_batch,
        _fake_narrative_executor,
    )
    from tests.integration.test_monitor_period_changes import VALUES, _two_period_read_value

    batches: dict[str, list[frozenset[str]]] = {"locate": [], "read_value": []}

    def recording(executor, name):
        def _wrapped(items, ctx):
            batches[name].append(frozenset(args.concept for args in items))
            return executor(items, ctx)
        return _wrapped

    locate_exec, _ = make_locate({concept: 1.0 for concept in (VALUES if found else {})})
    read_value = _two_period_read_value if found else (lambda items, ctx: {a.concept: () for a in items})
    _patched_build_registry(
        monkeypatch, recording(locate_exec, "locate"), recording(read_value, "read_value"),
        structure_executor=make_structure({}), entities_executor=make_entities([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor, narrative_executor=_fake_narrative_executor,
    )
    return batches


def _monitor_intent(monkeypatch):
    _intent(monkeypatch, {
        "intent_kind": "monitor_changes", "targets": [], "entity_ref": None,
        "period_ref": None, "role": None, "unresolved": [], "ambiguities": [],
    })


def _full_analysis_intent(monkeypatch):
    _intent(monkeypatch, {
        "intent_kind": "full_analysis", "targets": [], "entity_ref": None,
        "period_ref": None, "role": "other", "unresolved": [], "ambiguities": [],
    })


def test_monitor_changes_in_an_empty_chat_extracts_computes_and_reports_period_changes(
    monkeypatch, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    """Живой отказ 25.09.2026: «Что изменилось у Лукойла?» на свежей сессии давал
    `monitor_case_unavailable`. Case и паспорт заранее не нужны: конвейер сам
    извлекает и считает по ячейке, затем код сравнивает периоды внутри отчёта, а
    модель объясняет (Срез 32, T4)."""
    _monitor_intent(monkeypatch)
    batches = _arm_monitor_env(
        monkeypatch, make_fake_locate_and_read_value_executors,
        make_fake_structure_executor, make_fake_entities_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    graph, result = _assert_query_stream_terminal(
        turn.run("Что изменилось у Лукойла?", role="other"), "PeriodChangeReport",
    )

    assert graph["root"]["atom_name"] == "out.changes"
    assert batches["locate"] and batches["read_value"], "пустой чат: извлечение выполнено"
    rows = {row["concept"]: row for row in result["rows"]}
    assert rows["SALES_REVENUE"]["relative_change_pct"] == 25.0
    assert rows["NET_INCOME"]["relative_change_pct"] == -25.0
    assert result["explanation_status"] == "ready" and result["explanation"]["text"]
    assert result["role"] == "other"
    assert turn.prompt == "", "готовый PeriodChangeReport не должен запускать второй модельный вызов"


def test_the_same_monitor_request_repeated_in_the_case_does_not_extract_again(
    monkeypatch, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    """T5, середина диалога: тот же запрос повторён в том же Case — план тот же, все его
    узлы уже успешно выполнены (идемпотентный `PlanRun`), извлечение не запускается.
    Отдельной ветки «середина диалога» в коде нет."""
    _monitor_intent(monkeypatch)
    batches = _arm_monitor_env(
        monkeypatch, make_fake_locate_and_read_value_executors,
        make_fake_structure_executor, make_fake_entities_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    _graph, first = _assert_query_stream_terminal(
        turn.run("Что изменилось у Лукойла?", role="other"), "PeriodChangeReport",
    )
    extracted_once = {name: list(calls) for name, calls in batches.items()}
    _graph, second = _assert_query_stream_terminal(
        turn.run("А что изменилось по сравнению с прошлым периодом?", role="other"),
        "PeriodChangeReport",
    )

    assert extracted_once["read_value"]
    assert batches == extracted_once, "повторный запрос в том же Case не читает отчёт заново"
    assert second["rows"] == first["rows"], "таблица изменений воспроизводима"


def test_monitor_changes_after_a_full_analysis_reuses_the_values_it_already_found(
    monkeypatch, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    """T5: типичный предшественник — полный анализ той же роли (одна методика в паспорте,
    один ролевой набор). Найденные им значения берутся из Case: в `locate`/`read.value`
    больше не уходит ни один показатель, который уже найден."""
    from tests.integration.test_monitor_period_changes import VALUES

    batches = _arm_monitor_env(
        monkeypatch, make_fake_locate_and_read_value_executors,
        make_fake_structure_executor, make_fake_entities_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    _full_analysis_intent(monkeypatch)
    _assert_query_stream_terminal(turn.run("сделай полный финансовый анализ", role="other"), "ReportOutput")
    assert all(set(VALUES) <= batch for batch in batches["read_value"]), "анализ искал оба показателя"
    already = {name: len(calls) for name, calls in batches.items()}

    _monitor_intent(monkeypatch)
    _graph, result = _assert_query_stream_terminal(
        turn.run("Что изменилось?", role="other"), "PeriodChangeReport",
    )

    for name, calls in batches.items():
        for batch in calls[already[name]:]:
            assert not set(VALUES) & batch, f"найденное повторно ушло в {name}: {sorted(set(VALUES) & batch)}"
    assert {row["concept"] for row in result["rows"]} >= set(VALUES)


@pytest.mark.xfail(strict=True, reason=(
    "Срез 32, Nedodelano: результат «не найдено» не сохраняется в Case, поэтому показатели, "
    "которых нет в отчёте, повторно уходят в locate/read.value при каждой новой цели. "
    "Снять xfail, когда появится сохраняемый маркер «искали, не нашли»."
))
def test_monitor_changes_after_a_full_analysis_does_not_search_again_for_what_is_absent(
    monkeypatch, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    """Требование владельца, пункт 3, в строгой форме: если извлечение и расчёты уже
    зафиксированы — мониторинг не запускает извлечение вовсе."""
    batches = _arm_monitor_env(
        monkeypatch, make_fake_locate_and_read_value_executors,
        make_fake_structure_executor, make_fake_entities_executor,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    _full_analysis_intent(monkeypatch)
    _assert_query_stream_terminal(turn.run("сделай полный финансовый анализ", role="other"), "ReportOutput")
    already = {name: len(calls) for name, calls in batches.items()}

    _monitor_intent(monkeypatch)
    _assert_query_stream_terminal(turn.run("Что изменилось?", role="other"), "PeriodChangeReport")

    assert {name: len(calls) for name, calls in batches.items()} == already


def test_monitor_changes_with_nothing_to_compare_is_a_typed_refusal_that_says_so(
    monkeypatch, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    """В отчёте не нашлось ни одного показателя роли — честный типизированный отказ, а
    не пустая таблица и не общая фраза про «конвейер не завершён»."""
    _monitor_intent(monkeypatch)
    _arm_monitor_env(
        monkeypatch, make_fake_locate_and_read_value_executors,
        make_fake_structure_executor, make_fake_entities_executor, found=False,
    )
    turn = _Turn()
    turn.arm(monkeypatch)

    events = turn.run("Что изменилось у Лукойла?", role="other")

    errors = [event["error"] for event in events if event.get("type") == "typed_error"]
    assert [error["code"] for error in errors] == ["changes_not_found"]
    assert not any(event.get("type") == "typed_result" for event in events)
    assert "текст отчёта ЛУКОЙЛ" not in turn.prompt
