from __future__ import annotations

import pytest

from finengine.contracts import NarrativeOutput
from finengine.contracts import NodeKey, NodeRun
from finengine.contracts import (
    ApproachReport,
    CalculatedMetric,
    DerivedFact,
    ExtractedMetric,
    MonitorResult,
    MonitorRunTrace,
    PeriodChangeReport,
    PeriodChangeRow,
    ScalarFact,
    SubjectDiff,
    SubjectKey,
)
from finengine.contracts.transport import (
    CompletedEvent,
    LegacyCompatibilityEvent,
    ResultEvent,
    ResultEvent,
    envelope_legacy_event,
)
from finengine.skills import run_orchestrator
from finengine.skills.result_events import node_run_event, typed_result_event, typed_results


def test_typed_result_and_completion_are_discriminated_v1_events():
    result = envelope_legacy_event(
        {"type": "typed_result", "status": "partial", "result": NarrativeOutput(
            run_id="run-1", status="partial", text="Частичный результат",
        ).model_dump(mode="json")},
        run_id="run-1",
        sequence=2,
    )
    done = envelope_legacy_event({"type": "done"}, run_id="run-1", sequence=3)
    assert isinstance(result, ResultEvent)
    assert result.schema_version == "1.0" and result.delivery_status == "partial"
    assert isinstance(done, CompletedEvent) and done.status == "succeeded"


def test_arbitrary_text_stays_opaque_during_migration():
    event = envelope_legacy_event(
        {"type": "text_delta", "text": '{"rows":[{"value":999}]}'},
        run_id="run-1",
        sequence=1,
    )
    assert isinstance(event, LegacyCompatibilityEvent)
    assert event.legacy_event["text"].startswith("{")


def test_progress_payload_is_the_real_node_run_state():
    run = NodeRun(
        node_key=NodeKey(atom_name="out.report", atom_version="1.0.0", args_fingerprint="abc"),
        run_id="run-1",
        status="running",
        sandbox_profile="compute_no_network",
    )
    event = node_run_event(run, plan_graph_hash="sha256:graph")
    assert event["type"] == "node_run"
    assert event["node_run"] == run.model_dump(mode="json")
    assert "text" not in event and "steps" not in event


@pytest.mark.asyncio
async def test_reconnect_cursor_replays_only_new_events_in_order(monkeypatch):
    source = [
        {"type": "text_delta", "text": "one"},
        {"type": "typed_result", "result": NarrativeOutput(run_id="run-1", text="result").model_dump(mode="json")},
        {"type": "done"},
    ]

    async def fake_subscribe(*_args, **_kwargs):
        for event in source:
            yield event

    monkeypatch.setattr(run_orchestrator, "subscribe", fake_subscribe)
    replay = [event async for event in run_orchestrator.subscribe_versioned(
        "run-1", kind="chat", after_sequence=1,
    )]

    assert [event.sequence for event in replay] == [2, 3]
    assert [event.event_type for event in replay] == ["result", "completed"]


@pytest.mark.asyncio
async def test_error_with_partial_result_finishes_as_partial_even_after_reconnect(monkeypatch):
    partial = NarrativeOutput(run_id="run-1", status="partial", text="Доступная часть")
    source = [
        {
            "type": "typed_error",
            "error": {"code": "one_branch_failed", "message": "Одна ветвь не завершилась"},
            "partial_results": [partial.model_dump(mode="json")],
        },
        {"type": "done"},
    ]

    async def fake_subscribe(*_args, **_kwargs):
        for event in source:
            yield event

    monkeypatch.setattr(run_orchestrator, "subscribe", fake_subscribe)
    replay = [event async for event in run_orchestrator.subscribe_versioned(
        "run-1", kind="chat", after_sequence=1,
    )]

    assert len(replay) == 1
    assert isinstance(replay[0], CompletedEvent)
    assert replay[0].status == "partial"


def _scalar_fact(concept: str) -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref="doc-1#p7", concept=concept, value=125.0, unit="RUB_MLN",
        period_ref="2025",
    )


def _derived_fact(concept: str, inputs: list[str]) -> DerivedFact:
    return DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0",
        source_ref="calc:1", concept=concept, formula_id=concept,
        formula_version="1.0.0", inputs=inputs, value=1.31, unit=None,
    )


def _monitor_result() -> MonitorResult:
    key = SubjectKey(value="issuer-1", entity_id="issuer-1", identifiers={"inn": "1234567890"})
    diff = SubjectDiff(
        subject_key=key, previous_snapshot_id="snap-1", current_snapshot_id="snap-2",
        previous_plan_run_id="run-1", current_plan_run_id="run-2", comparability="comparable",
    )
    return MonitorResult(
        subject_key=key, diff=diff,
        approaches=ApproachReport(policy_id="default", policy_version="1.0.0"),
        trace=MonitorRunTrace(
            monitor_plan_run_id="run-3", source_snapshot_ids=["snap-1", "snap-2"],
            source_plan_run_ids=["run-1", "run-2"],
        ),
    )


def _period_change_report() -> PeriodChangeReport:
    return PeriodChangeReport(
        run_id="run-1", status="partial", role="other",
        rows=[PeriodChangeRow(
            row_id="changes:NET_INCOME", order=0, concept="NET_INCOME", label="Чистая прибыль",
            status="no_prior_period", reason="предыдущий период для сравнения не найден в отчёте",
        )],
        explanation_status="unavailable", explanation_note="объяснение не сформировано",
    )


def test_typed_results_recognizes_the_three_metric_and_monitor_root_types():
    """`ExtractedMetric`/`CalculatedMetric`/`MonitorResult`/`PeriodChangeReport` must pass the
    same `typed_results()` filter that already recognizes `ReportOutput`/
    `FormOutput` — otherwise a graph root of these types is built but never
    reaches the station listener as a `typed_result` event (Срез 27, Срез 32)."""
    metric = ExtractedMetric(scalar_fact=_scalar_fact("REVENUE"), concept="REVENUE", period_ref="2025")
    calculated = CalculatedMetric(
        derived_fact=_derived_fact("DEP_TO_CASH", [metric.scalar_fact.evidence_id]),
        concept="DEP_TO_CASH", period_ref="2025",
    )
    monitor = _monitor_result()
    changes = _period_change_report()

    results = typed_results([metric, calculated, monitor, changes, object()])

    assert results == (metric, calculated, monitor, changes)


def test_a_partial_period_change_report_is_delivered_as_a_partial_typed_result():
    event = typed_result_event(_period_change_report())
    assert event["status"] == "partial"
    assert event["result"]["explanation_status"] == "unavailable"


@pytest.mark.parametrize("factory,output_type", [
    (lambda: ExtractedMetric(scalar_fact=_scalar_fact("REVENUE"), concept="REVENUE", period_ref="2025"), "ExtractedMetric"),
    (
        lambda: CalculatedMetric(
            derived_fact=_derived_fact("DEP_TO_CASH", ["ev-1"]), concept="DEP_TO_CASH", period_ref="2025",
        ),
        "CalculatedMetric",
    ),
    (_monitor_result, "MonitorResult"),
    (_period_change_report, "PeriodChangeReport"),
])
def test_typed_result_event_carries_the_output_type_discriminant(factory, output_type):
    event = typed_result_event(factory())
    assert event["type"] == "typed_result"
    assert event["result"]["output_type"] == output_type
    enveloped = envelope_legacy_event(event, run_id="run-typed-root", sequence=1)
    assert isinstance(enveloped, ResultEvent)
    assert enveloped.result.output_type == output_type
