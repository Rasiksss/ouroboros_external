import json
import logging
import threading
import time
from types import SimpleNamespace

from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent.loop_tool_execution import ToolCallExecutor
from ouroboros.contracts.attachments import StoredFile
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import make_catalog, make_tool_ctx, make_tool_env
from tests.helpers.user_settings import StubMcpServerIdsReader

_extract_result_metadata = ToolCallExecutor._extract_result_metadata
_failure_class_for_result = ToolCallExecutor._failure_class_for_result
_is_tool_execution_failure = ToolCallExecutor._is_tool_execution_failure

#: Bounds an assertion, not the behavior under test: the abandoned ``send_file`` body
#: reaches the transport only after ``validate_attachment_path`` spawns its parser child,
#: and that spawn costs a full interpreter start — seconds on a loaded CI runner.
SEND_TIMEOUT = 30.0


def _await_abandoned_body(prefix: str = "tool-send_file") -> bool:
    """Whether the abandoned tool thread finished before the deadline.

    A body that outlived its deadline keeps running on its own thread — that abandonment is
    the runtime's design (``new_core/tools/runtimes.py``), not a defect. A test that releases
    such a body has to wait for the thread itself, not for a flag the body sets from inside:
    everything after that flag — the tool's result, the line it logs — still runs, and a test
    that returned already leaves those writes landing in a capture stream pytest has closed.
    That surfaces as a logging error belonging to no test at all.
    """
    deadline = time.monotonic() + SEND_TIMEOUT
    while time.monotonic() < deadline:
        if not any(thread.name.startswith(prefix) and thread.is_alive() for thread in threading.enumerate()):
            return True
        time.sleep(0.01)
    return False


def test_review_blocked_is_not_treated_as_tool_failure():
    assert not _is_tool_execution_failure(True, "⚠️ REVIEW_BLOCKED: reviewers unavailable")


def test_domain_errors_are_not_treated_as_tool_failures():
    assert not _is_tool_execution_failure(True, "⚠️ GIT_ERROR (commit): hook rejected commit")
    assert not _is_tool_execution_failure(True, "⚠️ SAFETY_VIOLATION: blocked by sandbox")


def test_executor_failures_are_still_tool_failures():
    assert _is_tool_execution_failure(False, "anything")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_ERROR (repo_commit): boom")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_TIMEOUT (run_shell): exceeded 120s")


def test_missing_skill_surface_is_a_tool_failure_with_a_stable_code():
    result = "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"

    assert _is_tool_execution_failure(True, result, tool_name="list_skills")
    assert _extract_result_metadata("list_skills", result, True)["error_code"] == "SKILLS_UNAVAILABLE"


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


class _CallableOutbound(ToolOutbound):
    """An outbound boundary whose publish is one function a test supplies."""

    def __init__(self, publish) -> None:
        self._publish = publish

    def send_file(self, media: StoredFile, **kwargs):
        return self._publish(media, **kwargs)

    def send_image(self, media: StoredFile, **kwargs):
        return self._publish(media, **kwargs)


def _send_file_env(tmp_path, outbound):
    """A catalog and context wired for send_file, with the outbound boundary injected.

    The boundary is handed to the context rather than reached for by the tool, so a test
    decides the publish's outcome without a supervisor in the picture.
    """
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    workspace = tmp_path / "data" / "tmp" / "task-workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.txt"
    report.write_text("report", encoding="utf-8")
    ctx.task_workspace = workspace
    ctx.outbound = outbound
    return catalog, ctx, report


def test_delivery_unknown_result_blocks_identical_send_before_handler(tmp_path):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_ledger import RepeatToolGuard
    from ouroboros.contracts.media_delivery import DELIVERY_UNKNOWN
    from tests.helpers.shared import run_tool_calls

    calls = {"count": 0}

    def _sender(*_args, **_kwargs):
        calls["count"] += 1
        return False, DELIVERY_UNKNOWN

    catalog, ctx, report = _send_file_env(tmp_path, _CallableOutbound(_sender))
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
    from ouroboros.storage import attachment_validation as attachments
    from tests.helpers.shared import run_tool_calls

    # send_file validates the document in a spawned interpreter; that spawn alone can
    # outlast the seconds this test gives the abandoned call to reach the outbound.
    monkeypatch.setattr(attachments, "run_parser", lambda *_args, **_kwargs: "")

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = {"count": 0}

    def _sender(*_args, **_kwargs):
        calls["count"] += 1
        started.set()
        release.wait(timeout=5)
        finished.set()
        return True, "message-1"

    catalog, ctx, report = _send_file_env(tmp_path, _CallableOutbound(_sender))
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

        assert started.wait(timeout=SEND_TIMEOUT)
        assert calls["count"] == 1
        assert "SEND_FILE_DELIVERY_UNKNOWN" in messages[-2]["content"]
        assert "TOOL_REPEAT_BLOCKED" in messages[-1]["content"]
    finally:
        release.set()
        assert finished.wait(timeout=SEND_TIMEOUT)
        assert _await_abandoned_body(), "the abandoned send_file body outlived the test"


def test_shell_failures_are_treated_as_tool_failures():
    assert _is_tool_execution_failure(
        True,
        "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=1.\n\nSTDERR:\nboom",
    )


def test_skill_exec_failures_are_tool_failures():
    assert _is_tool_execution_failure(True, "⚠️ SKILL_EXEC_FAILED: skill 'alpha' script 'run.py' exited with code 2.")


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
    from new_core.tools.providers import McpToolProvider
    from ouroboros.mcp.connection import format_mcp_tool_error
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety
    from tests.helpers.shared import run_tool_calls

    _, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    full_name = "mcp__srv__boom"
    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="srv")],
                server_ids_reader=StubMcpServerIdsReader(["srv"]),
                auth_dependencies=null_egress_auth_dependencies(),
            )
        ]
    )
    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "resolve", lambda name: ("srv", "boom") if name == full_name else None)
    monkeypatch.setattr(
        mcp_manager,
        "call_tool_sync",
        lambda *args, **kwargs: format_mcp_tool_error("boom", "timed out after 60s"),
    )
    monkeypatch.setattr(safety, "check_safety", permissive_safety)

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
    from new_core.tools.providers import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.mcp.connection import DiscoveredMCPTool, _build_mcp_tool_name
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety
    from ouroboros.storage.settings.schema import McpServerEntry
    from tests.helpers.shared import run_tool_calls

    _, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    original_name = "query/employees"
    full_name = _build_mcp_tool_name("srv", original_name)
    default_name = _build_mcp_tool_name("srv", "overview")
    result = "x" * 30_500
    entry = McpServerEntry(id="srv", url="https://srv.example/mcp", tool_result_limits={original_name: 30_000})
    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[entry],
                server_ids_reader=StubMcpServerIdsReader(["srv"]),
                auth_dependencies=null_egress_auth_dependencies(),
            )
        ]
    )
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
    monkeypatch.setattr(safety, "check_safety", permissive_safety)

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


_SKILL_EXEC_SCRIPT_SKILL = """---
name: alpha
description: test
version: 0.1
type: script
runtime: python3
permissions: [fs, net]
scripts:
  - name: scripts/run.py
---
"""


@parametrize_from_dict(
    {
        "configured limit": {"configured": "500", "expected_limit": 500},
        "zero keeps the skill_exec default": {"configured": "0", "expected_limit": 120_000},
    }
)
def test_skill_exec_result_over_limit_is_tool_error(
    tmp_path, monkeypatch, isolate_settings, configured, expected_limit
):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy import safety
    from tests.helpers.shared import run_tool_calls

    monkeypatch.setenv("OUROBOROS_SKILL_EXEC_RESULT_LIMIT", configured)
    isolate_settings.bootstrap_config(force=True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    skill_dir = tmp_path / "repo" / "skills" / "alpha"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_EXEC_SCRIPT_SKILL, encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(
        "import os, pathlib\n"
        "(pathlib.Path(os.environ['OUROBOROS_SKILL_WORKDIR']) / 'out.txt').write_text('built')\n"
        "print('x' * 130000)\n",
        encoding="utf-8",
    )
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    ctx.task_id = "task-1"
    arguments = json.dumps({"skill": "distribution:alpha", "script": "scripts/run.py"})
    call = ToolCall(id="call-1", function=ToolCallFunction(name="skill_exec", arguments=arguments))
    messages: list[dict] = []

    errors = run_tool_calls([call], catalog, tmp_path / "logs", "task-1", messages, {"tool_calls": []}, ctx=ctx)

    content = messages[-1]["content"]
    assert errors == 1
    assert content.startswith("⚠️ TOOL_ERROR (skill_exec):")
    assert f"limit is {expected_limit}" in content
    assert "uploads/task_artifacts/task-1/out.txt" in content
    assert "xxxx" not in content


def test_failed_send_file_records_delivery_stage_and_one_safe_warning(tmp_path, caplog):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_ledger import TaskLedger
    from tests.helpers.shared import run_tool_calls

    drive_root = tmp_path / "data"
    invocations = sql_execution_memory_repository()
    catalog, ctx = make_tool_env(tmp_path / "repo", drive_root)
    workspace = drive_root / "tmp" / "task-workspace"
    ctx.task_workspace = workspace
    ctx.task_id = "task-1"
    ctx.outbound = RecordingOutbound(outcome=(False, "offline"))
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
            execution_memory_repository=invocations,
        )

    assert errors == 1
    assert invocations.list_tool_invocations(task_id="task-1")[0].status == "error"
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
    from new_core.tools.entry import ToolEntry
    from new_core.tools.schema import ToolSchema
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests.helpers.shared import collecting_event_sink, make_catalog, static_tool_provider

    entry = ToolEntry(
        name="data_read",
        schema=ToolSchema(name="data_read", description="d", parameters={}),
        handler=lambda _ctx, **_kw: "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=7 signal=SIGTERM",
    )
    catalog = make_catalog([static_tool_provider(entry)])
    ctx = make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    events = collecting_event_sink()
    call = ToolCall(id="call-log", function=ToolCallFunction(name="data_read", arguments='{"path":"note.md"}'))

    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=events,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    executor.execute([call], ctx=ctx, task_id="task-log", decisions={})

    finished = next(payload for payload in events.log_payloads if payload["type"] == "tool_call_finished")
    assert finished["status"] == "non_zero_exit"
    assert finished["exit_code"] == 7
    assert finished["signal"] == "SIGTERM"
