from types import SimpleNamespace

import ouroboros.agent.task_pipeline as pipeline


def test_emit_task_results_queues_restart_after_final_events(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_store_task_result", lambda *args, **kwargs: None)

    pending_events = []
    ctx = SimpleNamespace(pending_restart_reason="apply timeout fix")
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline.emit_task_results(
        env=env,
        llm=object(),
        pending_events=pending_events,
        task={"id": "task-1", "type": "task", "chat_id": 1, "text": "do it"},
        text="All done",
        usage={"rounds": 2},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        ctx=ctx,
    )

    assert [evt["type"] for evt in pending_events] == [
        "send_message",
        "task_metrics",
        "task_done",
        "restart_request",
    ]
    assert pending_events[-1]["reason"] == "apply timeout fix"
    assert ctx.pending_restart_reason is None


def test_build_trace_summary_shows_structured_failure_facts():
    trace = {
        "tool_calls": [
            {
                "tool": "run_shell",
                "args": {"cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"]},
                "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
                "is_error": True,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
            }
        ],
        "reasoning_notes": ["Thought this might still work."],
    }

    summary = pipeline.build_trace_summary(trace)

    assert "status=non_zero_exit" in summary
    assert "exit_code=-9" in summary
    assert "signal=SIGKILL" in summary
    assert "Agent notes (supplementary, not source of truth)" in summary


def test_store_task_result_persists_review_evidence(tmp_path):
    env = SimpleNamespace(drive_root=tmp_path)

    pipeline._store_task_result(
        env=env,
        task={"id": "task-store", "type": "task", "text": "hi"},
        text="done",
        usage={"rounds": 2},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing={},
        review_evidence={"has_evidence": True, "open_obligations": [{"item": "tests_affected"}]},
    )

    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import load_task_result

    init_runtime_store(tmp_path)
    payload = load_task_result(tmp_path, "task-store")
    assert payload is not None
    assert payload["review_evidence"]["has_evidence"] is True
    assert payload["review_evidence"]["open_obligations"][0]["item"] == "tests_affected"


def test_store_task_result_preserves_failed_status(tmp_path):
    """A body finishing after the record went terminal records its trace, not its verdict.

    This write door bypasses the store's absorbing-status guard, so it applies the rule itself.
    """
    from ouroboros.storage.task_results import STATUS_FAILED, load_task_result, write_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    write_task_result(tmp_path, "task-failed", STATUS_FAILED, result="initial failure")
    closed = load_task_result(tmp_path, "task-failed")

    pipeline._store_task_result(
        env=env,
        task={"id": "task-failed", "type": "task", "text": "hi"},
        text="final failure reply",
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing=closed,
        review_evidence={},
    )

    payload = load_task_result(tmp_path, "task-failed")
    assert payload is not None
    assert payload["status"] == STATUS_FAILED
    assert payload["result"] == "initial failure"
    assert payload["trace_summary"]
    # The completion time is the one the supervisor recorded, not the late body's return.
    assert payload["ts"] == closed["ts"]


def test_store_task_result_rereads_the_record_before_writing(tmp_path):
    """The caller's snapshot is taken before publication; the sweep may have closed the record since."""
    from ouroboros.storage.task_results import STATUS_FAILED, STATUS_RUNNING, load_task_result, write_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    write_task_result(tmp_path, "task-late", STATUS_RUNNING, result="Task is running.")
    stale = load_task_result(tmp_path, "task-late")
    write_task_result(tmp_path, "task-late", STATUS_FAILED, result="Task killed by hard timeout after 1800s.")

    pipeline._store_task_result(
        env=env,
        task={"id": "task-late", "type": "task", "text": "hi"},
        text="the answer",
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        existing=stale,
        review_evidence={},
    )

    payload = load_task_result(tmp_path, "task-late")
    assert payload is not None
    assert payload["status"] == STATUS_FAILED
    assert payload["result"] == "Task killed by hard timeout after 1800s."
    # The write still lands as the same status, so the late body's trace is kept.
    assert payload["trace_summary"]


def test_emit_task_results_skips_memory_distillation_for_failed_task(tmp_path):
    from ouroboros.storage.runtime_store import init_runtime_store
    from ouroboros.storage.task_results import STATUS_FAILED, write_task_result

    init_runtime_store(tmp_path)
    write_task_result(tmp_path, "task-failed", STATUS_FAILED, result="initial failure")
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline.emit_task_results(
        env=env,
        llm=object(),
        pending_events=[],
        task={"id": "task-failed", "type": "task", "chat_id": 1, "text": "do it"},
        text="⚠️ Error during processing: RuntimeError: " + "x" * 120,
        usage={"rounds": 1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
    )

    store = init_runtime_store(tmp_path)
    assert [r for r in store.list_tasks() if r.task_type == "memory_distillation"] == []


def test_truncate_with_notice_uses_utils_ssot():
    """_truncate_with_notice in agent_task_pipeline is now truncate_review_artifact from utils.
    Verify it truncates long strings and adds a visible omission note (no silent clipping)."""
    from ouroboros.utils import truncate_review_artifact

    # The alias in agent_task_pipeline should be the same object
    assert pipeline._truncate_with_notice is truncate_review_artifact

    short = "hello"
    assert pipeline._truncate_with_notice(short, 100) == short

    long_text = "x" * 200
    result = pipeline._truncate_with_notice(long_text, 50)
    assert result.startswith("x" * 50)
    assert "50" in result  # omission note mentions limit
    assert len(result) > 50  # note appended, not just raw slice

    # Handles None gracefully
    assert pipeline._truncate_with_notice(None, 10) == ""
