"""Versioned API/SSE contracts for final results and graph progress.

The compatibility event is intentionally opaque: it keeps old clients/runs
readable but never attempts to reconstruct a result from prose. New producers
must emit ``result``/``node_run``/``error`` directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from .base import Contract, NonEmptyStr, new_id, utc_now
from .evidence import CalculatedMetric, ExtractedMetric
from .execution import NodeRun
from .observation import MonitorResult
from .results import FileArtifact, FormOutput, NarrativeOutput, PeriodChangeReport, ReportOutput

EVENT_SCHEMA_VERSION = "1.0"

TypedResult = Annotated[
    ReportOutput | FormOutput | NarrativeOutput | FileArtifact
    | ExtractedMetric | CalculatedMetric | MonitorResult | PeriodChangeReport,
    Field(discriminator="output_type"),
]


class RunEventBase(Contract):
    schema_version: Literal["1.0"] = EVENT_SCHEMA_VERSION
    event_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    # Durable events start at 1. Heartbeats repeat the last durable cursor and
    # therefore legitimately use 0 before the first journal entry.
    sequence: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=utc_now)


class ResultEvent(RunEventBase):
    sequence: int = Field(ge=1)
    event_type: Literal["result"] = "result"
    delivery_status: Literal["complete", "partial"] = "complete"
    result: TypedResult


class NodeRunEvent(RunEventBase):
    sequence: int = Field(ge=1)
    event_type: Literal["node_run"] = "node_run"
    plan_graph_hash: NonEmptyStr
    node_run: NodeRun


class TypedError(Contract):
    code: NonEmptyStr
    message: NonEmptyStr
    retryable: bool = False
    node_key: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEvent(RunEventBase):
    sequence: int = Field(ge=1)
    event_type: Literal["error"] = "error"
    error: TypedError
    partial_results: list[TypedResult] = Field(default_factory=list)


class CompletedEvent(RunEventBase):
    sequence: int = Field(ge=1)
    event_type: Literal["completed"] = "completed"
    status: Literal["succeeded", "partial", "failed", "cancelled"]


class HeartbeatEvent(RunEventBase):
    event_type: Literal["heartbeat"] = "heartbeat"


class LegacyCompatibilityEvent(RunEventBase):
    sequence: int = Field(ge=1)
    event_type: Literal["legacy"] = "legacy"
    legacy_event: dict[str, Any]

    @model_validator(mode="after")
    def _legacy_cannot_smuggle_typed_results(self):
        if self.legacy_event.get("type") in {"typed_result", "node_run"}:
            raise ValueError("typed results/progress must use their versioned event contract")
        return self


RunEvent = Annotated[
    ResultEvent | NodeRunEvent | ErrorEvent | CompletedEvent | HeartbeatEvent | LegacyCompatibilityEvent,
    Field(discriminator="event_type"),
]
RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)


def envelope_legacy_event(event: dict[str, Any], *, run_id: str, sequence: int) -> RunEvent:
    """Upgrade a journal entry without inferring structure from free text."""
    event_type = event.get("type")
    if event_type == "typed_result":
        return ResultEvent(
            run_id=run_id,
            sequence=sequence,
            delivery_status=event.get("status", "complete"),
            result=event["result"],
        )
    if event_type == "node_run":
        return NodeRunEvent(
            run_id=run_id,
            sequence=sequence,
            plan_graph_hash=str(event["plan_graph_hash"]),
            node_run=event["node_run"],
        )
    if event_type == "typed_error":
        return ErrorEvent(
            run_id=run_id,
            sequence=sequence,
            error=event["error"],
            partial_results=event.get("partial_results") or [],
        )
    if event_type == "done":
        return CompletedEvent(run_id=run_id, sequence=sequence, status="succeeded")
    if event_type == "error":
        message = str(event.get("message") or event.get("error") or "run failed")
        return ErrorEvent(
            run_id=run_id,
            sequence=sequence,
            error=TypedError(code="legacy_error", message=message),
        )
    return LegacyCompatibilityEvent(run_id=run_id, sequence=sequence, legacy_event=dict(event))


__all__ = [
    "EVENT_SCHEMA_VERSION", "TypedResult", "ResultEvent", "NodeRunEvent", "TypedError",
    "ErrorEvent", "CompletedEvent", "HeartbeatEvent", "LegacyCompatibilityEvent", "RunEvent",
    "RUN_EVENT_ADAPTER", "envelope_legacy_event",
]
