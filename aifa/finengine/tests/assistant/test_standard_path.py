"""Стандартный путь `aifa_goal`: разбор цели, конвейер, этап 2 (CONTRACTS.md §3.1, ADR-0029).

Настоящие `parse_message`, `execute_routed_goal`, `GraphExecutor`, `CaseRuntime` (временная SQLite);
подставные только границы: модель разбора, модель планировщика цикла, пара `locate`/`read.value`,
хранилище документов. Опоры — те же, что у сквозных тестов чата (`tests/integration`).
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.conftest import (
    _make_fake_locate_and_read_value_executors,
    severstal_text,  # noqa: F401 — фикстура для `gate_env`
)
from tests.integration.ingress_support import (  # noqa: F401 — `gate_env`: autouse-окружение
    DOC,
    OWNER,
    FakeMeta,
    FakeStore,
    gate_env,
    patched_build_registry,
    set_intent,
    set_intent_sequence,
)

from finengine.assistant import standard_path
from finengine.assistant.goal_result import Verdict
from finengine.assistant.standard_path import StandardPath
from finengine.assistant.turn_record import TurnRecord

REVENUE = {
    "intent_kind": "extract_registered_metric", "targets": [{"concept": "SALES_REVENUE"}],
    "entity_ref": None, "period_ref": "p-2024-fy", "role": None, "unresolved": [], "ambiguities": [],
}
CUSTOM = {
    "route": "plan", "intent_kind": "custom_analysis", "targets": [], "entity_ref": None,
    "period_ref": None, "role": None, "unresolved": ["сложный вопрос"], "ambiguities": [],
}
GIVE_UP = {
    "objective": "остановиться", "reason": "данных нет", "action": "give_up", "scenarios": [],
    "new_atoms": [], "steps": [], "answer": "Данных для ответа в отчёте нет.", "evidence_ids": [],
}
MESSAGE = "какая выручка за 2024 год?"


@pytest.fixture(autouse=True)
def documents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(standard_path, "get_store", lambda: FakeStore({DOC: FakeMeta(OWNER)}))


def record_of(**changes: Any) -> TurnRecord:
    fields: dict[str, Any] = {
        "run_id": "run-1", "owner_id": str(OWNER), "chat_id": "p-lukoil", "message": MESSAGE,
        "user_id": OWNER, "case_id": "p-lukoil", "doc_ids": (DOC,), "active_doc_id": "", "role": "",
    }
    fields.update(changes)
    return TurnRecord(**fields)


def kinds(events: list[dict]) -> list[str]:
    return [str(event.get("type")) for event in events]


def metric_values(rows: tuple[dict, ...]) -> list[float]:
    return [
        payload.scalar_fact.value
        for row in rows
        for payload in row["payloads"]
        if row["output_type"] == "ExtractedMetric"
    ]


def with_revenue(monkeypatch: pytest.MonkeyPatch, value: float | None = 987_654.0) -> None:
    locate, read_value = _make_fake_locate_and_read_value_executors(
        {} if value is None else {"SALES_REVENUE": value}
    )
    patched_build_registry(monkeypatch, locate, read_value)


async def test_a_metric_goal_runs_the_deterministic_graph_and_publishes_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_intent(monkeypatch, REVENUE)
    with_revenue(monkeypatch)
    events: list[dict] = []

    result = await StandardPath()(record_of(), events.append)

    assert result.verdict is Verdict.ANSWERED
    assert set(metric_values(result.rows)) == {987_654.0}
    assert kinds(events).count("plan_graph") == 1, "тот же поток событий, что у чата: план один раз"
    assert "node_run" in kinds(events) and kinds(events).count("typed_result") == 1
    assert "typed_error" not in kinds(events)


async def test_the_goal_is_the_message_of_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0004: разбирается исходное сообщение прогона; в запрос к модели попадает оно и документы хода."""
    recorder = set_intent_sequence(monkeypatch, [REVENUE])
    with_revenue(monkeypatch)

    await StandardPath()(record_of(message="выручка за 2024 год, пожалуйста"), lambda event: None)

    assert "выручка за 2024 год, пожалуйста" in recorder.calls[0]["prompt"]
    assert "ЛУКОЙЛ" in recorder.calls[0]["prompt"], "название документа хода — контекст маршрутизатора"


async def test_a_question_that_needs_no_calculation_is_left_to_the_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_intent(monkeypatch, {"route": "answer", "intent_kind": None, "targets": [], "reason": "разговор"})
    events: list[dict] = []

    result = await StandardPath()(record_of(), events.append)

    assert (result.verdict, result.note) == (Verdict.NO_CALCULATION, "разговор")
    assert events == [], "конвейер не запускался"


async def test_a_calculation_with_no_document_asks_the_user_for_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_intent(monkeypatch, REVENUE)
    events: list[dict] = []

    result = await StandardPath()(record_of(doc_ids=()), events.append)

    assert result.verdict is Verdict.CLARIFY and "приложите отчёт" in result.note
    assert events[0]["error"]["code"] == "document_required", "экран получает тот же отказ, что и раньше"


async def test_several_documents_without_an_active_one_ask_to_choose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_intent(monkeypatch, REVENUE)
    events: list[dict] = []

    result = await StandardPath()(record_of(doc_ids=(DOC, "doc-2")), events.append)

    assert result.verdict is Verdict.CLARIFY and "активный отчёт" in result.note
    assert events[0]["error"]["code"] == "document_scope_ambiguous"


async def test_the_active_document_is_taken_among_several(monkeypatch: pytest.MonkeyPatch) -> None:
    set_intent(monkeypatch, REVENUE)
    with_revenue(monkeypatch)

    result = await StandardPath()(record_of(doc_ids=(DOC, "doc-2"), active_doc_id=DOC), lambda e: None)

    assert result.verdict is Verdict.ANSWERED and result.rows


async def test_a_document_without_text_is_a_refusal_not_an_empty_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_intent(monkeypatch, REVENUE)
    monkeypatch.setattr("finengine.store.blobs.document_text", lambda doc_id, **_: None)

    result = await StandardPath()(record_of(), lambda event: None)

    assert result.verdict is Verdict.NO_DOCUMENT


async def test_a_metric_missing_from_the_report_is_a_result_not_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.1: «показателя нет в отчёте» — результат; цикл (этап 2) не открывается."""
    set_intent(monkeypatch, REVENUE)
    with_revenue(monkeypatch, None)
    events: list[dict] = []

    result = await StandardPath()(record_of(), events.append)

    assert result.verdict is Verdict.ANSWERED
    assert "не найден в отчёте" in result.note
    assert [e["error"]["code"] for e in events if e["type"] == "typed_error"] == ["metric_not_found"]
    assert "dynamic_move" not in kinds(events)


async def test_an_ambiguous_goal_is_a_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    set_intent(monkeypatch, {
        **REVENUE, "targets": [],
        "ambiguities": [{"term": "прибыль", "candidates": ["NET_INCOME", "EBITDA_MARGIN"], "reason": "?"}],
    })
    events: list[dict] = []

    result = await StandardPath()(record_of(message="какая прибыль?"), events.append)

    assert result.verdict is Verdict.CLARIFY and "прибыль" in result.note
    assert events[-1]["error"]["code"] == "goal_needs_clarification"


async def test_when_the_standard_path_cannot_plan_the_service_runs_the_cycle_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0029, этап 2: при `loop_driver=engine` (откат) цель без производителя в графе → `DynamicRun` целиком, итог — ответ."""
    monkeypatch.setattr("finengine.config.settings.loop_driver", "engine")
    set_intent(monkeypatch, CUSTOM)
    with_revenue(monkeypatch)
    planner = SequenceModel([GIVE_UP])
    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", planner)
    events: list[dict] = []

    result = await StandardPath()(record_of(), events.append)

    assert result.verdict is Verdict.ANSWERED
    assert result.note == "Данных для ответа в отчёте нет."
    assert len(planner.prompts) == 1, "планировщик цикла спрошен"
    assert "dynamic_move" in kinds(events)
    refusals = [e["error"]["code"] for e in events if e["type"] == "typed_error"]
    assert "scenario_not_deterministic" not in refusals, "отказ, после которого идёт цикл, на экран не попадает"


async def test_a_pipeline_that_crashes_is_reported_to_the_turn_and_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Системная ошибка: событие `evidence_gate_failed` в журнал хода (как в чате) и исключение вызвавшему."""
    set_intent(monkeypatch, REVENUE)

    def broken(**_: Any) -> Any:
        raise RuntimeError("реестр сломан")

    monkeypatch.setattr("finengine.skills.production_runtime.build_registry", broken)
    events: list[dict] = []

    with pytest.raises(RuntimeError):
        await StandardPath()(record_of(), events.append)

    assert [e["error"]["code"] for e in events if e["type"] == "typed_error"] == ["evidence_gate_failed"]


class SequenceModel:
    """Планировщик цикла по сценарию: i-й вопрос получает i-й ответ, последний повторяется."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def __call__(self, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.replies[min(len(self.prompts), len(self.replies)) - 1], ensure_ascii=False)


async def test_the_trace_of_the_turn_reaches_the_nodes_of_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0007: трасса хода доходит до контекста узла — оттуда её берёт комната станции (`X-Trace-Id`)."""
    seen: list[str] = []
    locate, read_value = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 1.0})

    def reading(items: Any, ctx: Any) -> Any:
        seen.append(ctx.inputs.extra.get("trace_id", ""))
        return read_value(items, ctx)

    set_intent(monkeypatch, REVENUE)
    patched_build_registry(monkeypatch, locate, reading)

    await StandardPath()(record_of(trace_id="trace-of-the-turn"), lambda event: None)

    assert seen and set(seen) == {"trace-of-the-turn"}
