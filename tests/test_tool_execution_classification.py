import json
import logging
import threading
from types import SimpleNamespace

from ouroboros.agent.loop_tool_execution import ToolCallExecutor
from tests._shared import make_tool_env

_extract_result_metadata = ToolCallExecutor._extract_result_metadata
_failure_class_for_result = ToolCallExecutor._failure_class_for_result
_is_tool_execution_failure = ToolCallExecutor._is_tool_execution_failure


def test_review_blocked_is_not_treated_as_tool_failure():
    assert not _is_tool_execution_failure(True, "⚠️ REVIEW_BLOCKED: reviewers unavailable")


def test_domain_errors_are_not_treated_as_tool_failures():
    assert not _is_tool_execution_failure(True, "⚠️ GIT_ERROR (commit): hook rejected commit")
    assert not _is_tool_execution_failure(True, "⚠️ SAFETY_VIOLATION: blocked by sandbox")


def test_executor_failures_are_still_tool_failures():
    assert _is_tool_execution_failure(False, "anything")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_ERROR (repo_commit): boom")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_TIMEOUT (run_shell): exceeded 120s")


def test_file_tool_failures_have_error_codes_without_false_positives():
    for tool, error_code in (
        ("data_write", "DATA_WRITE_ERROR"),
        ("data_write", "SKILL_PAYLOAD_ARG_ERROR"),
        ("send_image", "SEND_IMAGE_NOT_FOUND"),
        ("send_image", "SEND_IMAGE_DELIVERY_ERROR"),
        ("send_photo", "SEND_IMAGE_DELIVERY_ERROR"),
        ("send_file", "SEND_FILE_NOT_FOUND"),
        ("send_file", "SEND_FILE_DELIVERY_ERROR"),
    ):
        result = f"⚠️ {error_code}: failure"
        assert _is_tool_execution_failure(True, result, tool_name=tool)
        assert _extract_result_metadata(tool, result, True)["error_code"] == error_code

    output = "OK: documentation\n⚠️ DATA_WRITE_ERROR: example only"
    assert not _is_tool_execution_failure(True, output, tool_name="data_read")
    assert _is_tool_execution_failure(True, output, tool_name="data_write")


def test_delivery_unknown_error_codes_are_repeat_guard_failure_classes():
    for error_code in ("SEND_FILE_DELIVERY_UNKNOWN", "SEND_IMAGE_DELIVERY_UNKNOWN"):
        result = f"⚠️ {error_code}: delivery unconfirmed"
        exec_result = {"result": result, "result_meta": _extract_result_metadata("send_file", result, True)}

        assert _failure_class_for_result(exec_result) == error_code

    ordinary = "⚠️ SEND_FILE_DELIVERY_ERROR: delivery failed"
    exec_result = {"result": ordinary, "result_meta": _extract_result_metadata("send_file", ordinary, True)}
    assert _failure_class_for_result(exec_result) == "error"


def _send_file_env(tmp_path, sender):
    """A catalog and context wired for send_file, with the media transport injected.

    The transport is a ``ctx.media_senders`` entry rather than anything the tool reaches
    for itself, so a test decides delivery's outcome without a supervisor in the picture.
    """
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    workspace = tmp_path / "data" / "tmp" / "task-workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.txt"
    report.write_text("report", encoding="utf-8")
    ctx.current_chat_id = "peer-42"
    ctx.task_workspace = workspace
    ctx.media_senders = {"file": sender}
    return catalog, ctx, report


def test_delivery_unknown_result_blocks_identical_send_before_handler(tmp_path):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_ledger import RepeatToolGuard
    from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
    from tests._shared import run_tool_calls

    calls = {"count": 0}

    def _sender(*_args, **_kwargs):
        calls["count"] += 1
        return False, DELIVERY_UNKNOWN

    catalog, ctx, report = _send_file_env(tmp_path, _sender)
    guard = RepeatToolGuard()
    arguments = json.dumps({"file_path": str(report)})
    messages: list[dict] = []

    for index in range(2):
        run_tool_calls(
            [ToolCall(id=f"call-{index}", function=ToolCallFunction(name="send_file", arguments=arguments))],
            catalog,
            tmp_path / "data" / "logs",
            "task-1",
            messages,
            {"tool_calls": []},
            lambda _text: None,
            ctx=ctx,
            repeat_guard=guard,
        )

    assert calls["count"] == 1
    assert "SEND_FILE_DELIVERY_UNKNOWN" in messages[-2]["content"]
    assert "TOOL_REPEAT_BLOCKED" in messages[-1]["content"]


def test_media_tool_timeout_is_delivery_unknown_and_blocks_late_duplicate(tmp_path, monkeypatch):
    import ouroboros.agent.loop_tool_execution as loop_tool_execution
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_ledger import RepeatToolGuard
    from tests._shared import run_tool_calls

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = {"count": 0}

    def _sender(*_args, **_kwargs):
        calls["count"] += 1
        started.set()
        release.wait(timeout=1)
        finished.set()
        return True, "message-1"

    catalog, ctx, report = _send_file_env(tmp_path, _sender)
    guard = RepeatToolGuard()
    monkeypatch.setattr(loop_tool_execution.ToolCallExecutor, "_get_tool_timeout", lambda *_args: 0.01)
    arguments = json.dumps({"file_path": str(report)})
    messages: list[dict] = []

    try:
        for index in range(2):
            errors = run_tool_calls(
                [ToolCall(id=f"call-{index}", function=ToolCallFunction(name="send_file", arguments=arguments))],
                catalog,
                tmp_path / "data" / "logs",
                "task-1",
                messages,
                {"tool_calls": []},
                lambda _text: None,
                ctx=ctx,
                repeat_guard=guard,
            )
            assert errors == 1

        assert started.wait(timeout=1)
        assert calls["count"] == 1
        assert "SEND_FILE_DELIVERY_UNKNOWN" in messages[-2]["content"]
        assert "TOOL_REPEAT_BLOCKED" in messages[-1]["content"]
    finally:
        release.set()
        assert finished.wait(timeout=1)


def test_shell_failures_are_treated_as_tool_failures():
    assert _is_tool_execution_failure(
        True,
        "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=1.\n\nSTDERR:\nboom",
    )


def test_shell_regex_autocorrect_success_is_not_tool_failure():
    result = "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped alternation\nexit_code=0\nSTDOUT:\nmatch"
    assert not _is_tool_execution_failure(True, result)
    assert _extract_result_metadata("run_shell", result, False)["status"] == "ok_autocorrected"


def test_shell_regex_autocorrect_nonzero_still_fails():
    result = (
        "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped alternation\n"
        "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=2.\n\nSTDERR:\nboom"
    )
    assert _is_tool_execution_failure(True, result)
    assert _extract_result_metadata("run_shell", result, True)["status"] == "shell_error"


def test_mcp_tool_errors_are_treated_as_tool_failures():
    assert _is_tool_execution_failure(True, "⚠️ MCP_TOOL_ERROR (search): boom")
    assert _is_tool_execution_failure(True, "⚠️ MCP_TOOL_ERROR (search): timed out after 60s")


def test_mcp_tool_error_status_metadata():
    assert _extract_result_metadata("mcp__srv__search", "⚠️ MCP_TOOL_ERROR (search): boom", True)["status"] == "error"
    assert (
        _extract_result_metadata("mcp__srv__search", "⚠️ MCP_TOOL_ERROR (search): timed out after 60s", True)["status"]
        == "timeout"
    )


def test_mcp_tool_error_counts_as_error_via_handle_tool_calls(tmp_path, monkeypatch):

    import json

    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.mcp.connection import format_mcp_tool_error
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety
    from tests._shared import run_tool_calls

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    full_name = "mcp__srv__boom"
    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "resolve", lambda name: ("srv", "boom") if name == full_name else None)
    monkeypatch.setattr(
        mcp_manager,
        "call_tool_sync",
        lambda *args, **kwargs: format_mcp_tool_error("boom", "timed out after 60s"),
    )
    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))

    call = ToolCall(id="call-mcp", function=ToolCallFunction(name=full_name, arguments=json.dumps({})))
    messages: list[dict] = []
    trace: dict = {"tool_calls": []}

    errors = run_tool_calls(
        [call],
        catalog,
        tmp_path / "logs",
        "task-1",
        messages,
        trace,
        lambda _text: None,
        ctx=ctx,
    )

    assert errors == 1
    entry = trace["tool_calls"][-1]
    assert entry["is_error"] is True
    assert entry["status"] == "timeout"
    assert "MCP_TOOL_ERROR" in messages[-1]["content"]


def test_mcp_tool_result_uses_configured_limit(tmp_path, monkeypatch):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.schema import ToolSchema
    from ouroboros.mcp.connection import DiscoveredMCPTool, _build_mcp_tool_name
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety
    from ouroboros.storage.settings.schema import McpServerEntry
    from tests._shared import run_tool_calls

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    original_name = "query/employees"
    full_name = _build_mcp_tool_name("srv", original_name)
    default_name = _build_mcp_tool_name("srv", "overview")
    result = "x" * 30_500
    entry = McpServerEntry(id="srv", tool_result_limits={original_name: 30_000})
    monkeypatch.setitem(mcp_manager._connections, "srv", SimpleNamespace(entry=entry))
    for name, original in ((full_name, original_name), (default_name, "overview")):
        monkeypatch.setitem(
            mcp_manager._tool_index,
            name,
            DiscoveredMCPTool(
                server_id="srv",
                original_name=original,
                full_name=name,
                schema=ToolSchema(name=name, description=original, parameters={}),
            ),
        )
    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "call_tool_sync", lambda *args, **kwargs: result)
    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))

    for name, expected_limit in ((full_name, 30_000), (default_name, 15_000)):
        call = ToolCall(id=f"call-{expected_limit}", function=ToolCallFunction(name=name, arguments="{}"))
        messages: list[dict] = []

        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            {"tool_calls": []},
            lambda _text: None,
            ctx=ctx,
        )

        assert errors == 0
        assert messages[-1]["content"] == (
            "x" * expected_limit + f"\n... (truncated from 30500 chars, limit={expected_limit})"
        )


def test_failed_send_file_records_delivery_stage_and_one_safe_warning(tmp_path, caplog):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_ledger import TaskLedger
    from ouroboros.storage.runtime_store import init_runtime_store
    from tests._shared import run_tool_calls

    drive_root = tmp_path / "data"
    store = init_runtime_store(drive_root)
    catalog, ctx = make_tool_env(tmp_path / "repo", drive_root)
    workspace = drive_root / "tmp" / "task-workspace"
    ctx.current_chat_id = "peer-42"
    ctx.task_workspace = workspace
    ctx.task_id = "task-1"
    ctx.media_senders = {"file": lambda *_args, **_kwargs: (False, "offline")}
    ledger = TaskLedger()
    workspace.mkdir(parents=True)
    report = workspace / "report.txt"
    report.write_text("private report body", encoding="utf-8")
    call = ToolCall(
        id="call-send-file",
        function=ToolCallFunction(
            name="send_file",
            arguments=json.dumps({"file_path": str(report), "caption": "private file body"}),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="ouroboros.agent.loop_tool_execution"):
        errors = run_tool_calls(
            [call],
            catalog,
            drive_root / "logs",
            "task-1",
            [],
            {"tool_calls": []},
            lambda _: None,
            ctx=ctx,
            task_ledger=ledger,
        )

    assert errors == 1
    assert store.list_tool_invocations(task_id="task-1")[0].status == "error"
    assert "status=error" in ledger.render()
    records = [record for record in caplog.records if "event=tool_outcome_failed" in record.getMessage()]
    assert len(records) == 1
    operational = records[0].getMessage()
    assert all(
        field in operational
        for field in (
            "task_id=task-1",
            "tool=send_file",
            "status=error",
            "error_code=SEND_FILE_DELIVERY_ERROR",
            "stage=transport_delivery",
            "filename=report.txt",
        )
    )
    assert str(tmp_path) not in operational
    assert "private file body" not in operational


def test_live_tool_log_payload_includes_structured_result_metadata(tmp_path):
    """A shell-shaped failure reaches the live log as fields, not just as a message string.

    The round's ``EventSink`` is what carries the line out; asserting on its payload is what
    keeps ``status``/``exit_code``/``signal`` from silently collapsing back into free text.
    """
    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.context import ToolContext
    from new_core.tools.entry import ToolEntry
    from new_core.tools.schema import ToolSchema
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests._shared import collecting_event_sink, make_catalog, static_tool_provider

    entry = ToolEntry(
        name="data_read",
        schema=ToolSchema(name="data_read", description="d", parameters={}),
        handler=lambda _ctx, **_kw: "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=7 signal=SIGTERM",
    )
    catalog = make_catalog([static_tool_provider(entry)])
    ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    events = collecting_event_sink()
    call = ToolCall(id="call-log", function=ToolCallFunction(name="data_read", arguments='{"path":"note.md"}'))

    executor = build_tool_call_executor(catalog=catalog, drive_logs=tmp_path / "logs", events=events)
    executor.execute([call], ctx=ctx, task_id="task-log")

    finished = next(payload for payload in events.log_payloads if payload["type"] == "tool_call_finished")
    assert finished["status"] == "non_zero_exit"
    assert finished["exit_code"] == 7
    assert finished["signal"] == "SIGTERM"
