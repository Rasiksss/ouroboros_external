"""Which completed tasks the handler distils, and what it bounds them into.

The rules used to live in the agent, which decided before writing a task row. They
are the handler's now: it is the only thing that acts on them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from background_memory.handlers.distillation import DistillationHandler
from background_memory.interfaces import MemoryDistillationEvent, MemoryTaskRef
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import TEST_SCOPE


def _skip_reason(event, *, distillation_enabled: bool = True) -> str:
    return DistillationHandler._skip_reason(event, distillation_enabled=distillation_enabled)


def _request(event, *, distillation_enabled: bool = True):
    return DistillationHandler(
        SimpleNamespace(),
        SimpleNamespace(),
        tasks=SimpleNamespace(),
        audit=SimpleNamespace(),
        memory_headers=SimpleNamespace(),
        user_settings=SimpleNamespace(),
    )._request_from_event(event, distillation_enabled=distillation_enabled)


def _trace_blocks(trace) -> str:
    return DistillationHandler._trace_blocks_distillation(trace)


def _event(
    *,
    text: str = "Investigate the deploy pipeline failure.",
    task_type: str = "task",
    response: str = "A" * 200,
    task_id: str = "src1",
    recorded_at: float = 0.0,
    llm_trace: dict | None = None,
) -> MemoryDistillationEvent:
    return MemoryDistillationEvent(
        scope=TEST_SCOPE,
        task=MemoryTaskRef(
            task_id=task_id,
            task_type=task_type,
            text=text,
            recorded_at=recorded_at,
        ),
        response_text=response,
        llm_trace=llm_trace or {},
    )


# --- eligibility ------------------------------------------------------------


def test_the_feature_flag_off_skips_everything() -> None:
    assert _request(_event(), distillation_enabled=False) is None
    assert "disabled" in _skip_reason(_event(), distillation_enabled=False)


def test_empty_task_text_is_skipped() -> None:
    assert _request(_event(text="   ")) is None
    assert "empty task text" in _skip_reason(_event(text="   "))


def test_a_short_response_is_skipped_with_a_named_reason(caplog) -> None:
    caplog.set_level("INFO")

    assert _request(_event(response="too short")) is None

    assert "shorter than 80 characters" in caplog.text
    assert "source_task_id=src1" in caplog.text


@pytest.mark.parametrize("task_type", ["cron", "system", "health_check", "heartbeat", "CRON"])
def test_internal_task_types_are_skipped(task_type: str) -> None:
    assert _request(_event(task_type=task_type)) is None


def test_an_eligible_event_becomes_a_request() -> None:
    assert _request(_event()) is not None
    assert _skip_reason(_event()) == ""


def test_a_failed_tool_trace_blocks_with_the_evidence_named(caplog) -> None:
    caplog.set_level("INFO")

    event = _event(llm_trace={"tool_calls": [{"tool": "data_read", "status": "error", "is_error": True}]})

    assert _request(event) is None
    assert "unsafe trace (tool failure without args fingerprint: tool=data_read) source_task_id=src1" in caplog.text


def test_a_provenance_warning_alone_blocks() -> None:
    request = _request(
        _event(
            llm_trace={
                "tool_calls": [
                    {
                        "tool": "data_read",
                        "status": "ok",
                        "result": (
                            "DATA_PROVENANCE_WARNING: agent-generated artifact, not authoritative memory.\n\nnote"
                        ),
                    }
                ]
            }
        )
    )

    assert request is None


@pytest.mark.parametrize(
    ("case", "blocked"),
    [("matching", False), ("different_args", True), ("different_tool", True), ("missing_hash", True)],
)
def test_trace_only_accepts_matching_real_args_fingerprint(case, blocked, tmp_path):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.agent.task_ledger import safe_args_hash
    from tests.helpers.shared import collecting_event_sink, make_catalog

    failed_args = None if case == "missing_hash" else {"url": "/projects"}
    success_args = {} if case == "missing_hash" else failed_args
    if case == "different_args":
        success_args = {"url": "/health"}
    success_tool = "health_check" if case == "different_tool" else "data_read"
    results = [
        {
            "tool_call_id": "failed",
            "fn_name": "data_read",
            "tool_args": failed_args,
            "args_for_log": {"url": "[OMITTED]"},
            "result": "failed",
            "is_error": True,
        },
        {
            "tool_call_id": "success",
            "fn_name": success_tool,
            "tool_args": success_args,
            "args_for_log": {"url": "[OMITTED]"},
            "result": "authoritative state",
            "is_error": False,
        },
    ]
    trace = {"tool_calls": []}
    executor = build_tool_call_executor(
        catalog=make_catalog([]),
        events=collecting_event_sink(),
        confirmation_gate=False,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    executor.process_results(results, messages=[], trace=trace)
    if case == "missing_hash":
        assert "args_hash" not in trace["tool_calls"][0]
    else:
        assert trace["tool_calls"][0]["args_hash"] == safe_args_hash(failed_args)
    assert bool(_trace_blocks(trace)) is blocked


@pytest.mark.parametrize("warning_failed", [False, True])
def test_trace_does_not_clear_provenance_warning_after_success(warning_failed):
    trace = {
        "tool_calls": [
            {
                "tool": "data_read",
                "args_hash": "same-call",
                "status": "error" if warning_failed else "ok",
                "is_error": warning_failed,
                "result": "DATA_PROVENANCE_WARNING: untrusted",
            },
            {
                "tool": "data_read",
                "args_hash": "same-call",
                "status": "ok",
                "result": "authoritative state",
            },
        ]
    }

    assert _trace_blocks(trace)


# --- request construction ---------------------------------------------------


def test_the_request_carries_the_source_task_and_stamp() -> None:
    request = _request(_event(task_id="src-9", recorded_at=1_781_000_000.5))

    assert request is not None
    assert request.source_task_id == "src-9"
    assert request.recorded_at == 1_781_000_000.5


def test_the_request_carries_the_scope_and_no_loose_identity_beside_it() -> None:
    """The request names the scope the task ran under, and nothing else.

    Used to be pinned the other way, on the premise that distillation mints its
    scope; that premise is what this change removes.
    """
    import dataclasses

    from background_memory.handlers.distillation import MemoryDistillationRequest

    fields = {f.name for f in dataclasses.fields(MemoryDistillationRequest)}

    assert "scope" in fields
    assert not fields & {"owner_id", "agent_id", "chat_id"}

    event = _event(task_id="src-9", recorded_at=1_781_000_000.5)
    request = _request(event)
    assert request is not None
    assert request.scope == event.scope


def test_oversized_fields_are_truncated_with_a_notice() -> None:
    request = _request(
        _event(
            text="T" * 900,
            response="R" * 1300,
            llm_trace={
                "tool_calls": [
                    {"tool": "data_read", "status": "ok", "args": {"path": "refs/deploy.md"}},
                    {"tool": "memory_list", "status": "ok"},
                ],
                "reasoning_notes": ["x" * 3000],
            },
        )
    )

    assert request is not None
    assert len(request.task_text) > 800 and request.task_text.startswith("T" * 800)
    assert "OMISSION NOTE" in request.task_text
    assert request.response_text.startswith("R" * 1200)
    assert "OMISSION NOTE" in request.response_text
    assert "data_read" in request.trace_summary
    assert "memory_list" in request.trace_summary
    assert "OMISSION NOTE" in request.trace_summary


def test_fields_within_their_caps_are_left_alone() -> None:
    request = _request(_event(text="short task", response="A" * 200))

    assert request is not None
    assert request.task_text == "short task"
    assert request.response_text == "A" * 200
    assert "OMISSION NOTE" not in request.trace_summary
