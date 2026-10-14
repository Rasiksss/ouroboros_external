"""The run uses its composed outbound boundary; detached media waits for completion."""

from __future__ import annotations

import ast
import base64
import dataclasses
import pathlib
import time
from types import SimpleNamespace

from new_core.tools.context import ToolContext
from new_core.tools.interfaces import ToolOutbound
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.service.messaging import get_tools, send_image_data
from tests.helpers.agent_owner_session import sql_owner_session_repository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.fake_gateway import RecordingOutbox
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx
from tests.helpers.startup_usage import sql_startup_usage_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _tool(name: str):
    return next(entry.handler for entry in get_tools() if entry.name == name)


def _ctx(tmp_path, *, outbound=None) -> ToolContext:
    workspace = tmp_path / "data" / "tmp" / "task-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path / "data",
        scope=TEST_SCOPE,
        task_id="task-1",
        task_workspace=workspace,
        outbound=outbound,
    )


def test_a_run_holds_one_outbound_boundary_and_no_sender_dictionary() -> None:
    fields = {field.name: field for field in dataclasses.fields(ToolContext)}
    assert "media_senders" not in fields and fields["outbound"].type in (ToolOutbound | None, "ToolOutbound | None")


def test_the_agent_hands_the_boundary_it_was_given_to_each_run(tmp_path) -> None:
    from ouroboros.agent import make_agent
    from tests.helpers.shared import (
        make_empty_extension_runtime,
        make_empty_skill_snapshot,
        make_empty_skill_surface,
        make_skill_availability,
    )

    outbound = RecordingOutbound()
    agent = make_agent(
        str(tmp_path),
        str(tmp_path),
        skill_snapshot=make_empty_skill_snapshot(),
        skill_availability=make_skill_availability(),
        extension_runtime=make_empty_extension_runtime(),
        session_repository=sql_owner_session_repository(),
        startup_usage_repository=sql_startup_usage_repository(),
        outbound=outbound,
        execution_memory_repository=sql_execution_memory_repository(),
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        audit_event_repository=sql_audit_event_repository(),
    )
    ctx = agent._build_tool_context(
        {"id": "t-1"},
        None,
        root_task_id="t-1",
        scope=TEST_SCOPE,
        skill_surface=make_empty_skill_surface(),
    )
    assert ctx.outbound is outbound


def test_the_boundary_is_not_resolved_from_a_registry() -> None:
    """No module on a run's path to the outbox reads the process-local runtime registry."""
    roots = (
        pathlib.Path("ouroboros/tools/service/messaging.py"),
        pathlib.Path("ouroboros/agent/core.py"),
        *sorted(pathlib.Path("ouroboros/agent_runtime").rglob("*.py")),
    )
    offenders: list[str] = []
    for path in roots:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and node.id in {"require_runtime", "try_get_runtime", "try_get_bridge"}:
                offenders.append(f"{path}:{node.lineno} {node.id}")
    assert offenders == []


@parametrize_from_dict({"file": {"kind": "file"}, "image": {"kind": "image"}})
def test_a_publish_reports_acceptance_and_resends_the_same_file(tmp_path, kind) -> None:
    """The bridge persists and publishes; the channel is served later by a transport."""
    from supervisor.message_bus import LocalChatBridge

    drive_root = tmp_path / "data"
    runtime_persistence.initialize()
    outbox = RecordingOutbox()
    ctx = _ctx(tmp_path, outbound=LocalChatBridge(outbox, drive_root))
    report = ctx.task_workspace / "report.txt"
    report.write_text("report", encoding="utf-8")

    result = (
        send_image_data(ctx, b"\x89PNG\r\n\x1a\npayload", caption="report")
        if kind == "image"
        else _tool("send_file")(ctx, file_path=str(report), caption="report")
    )

    assert result.startswith("OK:")
    assert "accepted for delivery" in result
    assert len(outbox.published) == 1
    assert outbox.published[0].caption == "report"
    file_id = outbox.published[0].attachment.attachment_id
    provider = get_storage_provider()
    keys = provider.list_keys(ctx.scope, SCOPE_UPLOADS, "chat/")
    assert _tool("send_image" if kind == "image" else "send_file")(ctx, file_path=file_id).startswith("OK:")
    assert [message.attachment.attachment_id for message in outbox.published] == [file_id, file_id]
    assert provider.list_keys(ctx.scope, SCOPE_UPLOADS, "chat/") == keys
    with runtime_persistence.current.engine.begin() as conn, FileCatalog(conn, ctx.scope) as catalog:
        assert [entry.id for entry in catalog.entries(None).items] == [file_id]


@parametrize_from_dict(
    {
        "send_file": {"mode": "send_file"},
        "send_image": {"mode": "send_image"},
        "inline": {"mode": "inline"},
        "foreign": {"mode": "foreign"},
    }
)
def test_detached_media_stores_references_without_history_or_outbox_and_keeps_paths(tmp_path, mode):
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.tools.core import _send_photo
    from supervisor.message_bus import LocalChatBridge

    drive_root = tmp_path / "data"
    store = runtime_persistence.initialize()
    outbox = RecordingOutbox()
    ctx = dataclasses.replace(_ctx(tmp_path, outbound=LocalChatBridge(outbox, drive_root)), detached=True)
    content = b"report" if mode == "send_file" else b"\x89PNG\r\n\x1a\npayload"
    artifact = (tmp_path if mode == "foreign" else ctx.task_workspace) / (
        "report.txt" if mode == "send_file" else "chart.png"
    )
    artifact.write_bytes(content)
    result = (
        _send_photo(ctx, image_base64=base64.b64encode(content).decode())
        if mode == "inline"
        else _tool("send_image" if mode == "foreign" else mode)(ctx, file_path=str(artifact))
    )
    assert result.startswith("⚠️" if mode == "foreign" else "OK:")
    assert len(ctx.terminal_attachments) == (0 if mode == "foreign" else 1)
    assert artifact.read_bytes() == content
    assert (
        sql_conversation_repository(store.engine).list_messages(scope=TEST_SCOPE, include_progress=True) == []
        and outbox.published == []
    )
    from ouroboros.agent.task_pipeline import emit_task_results
    from ouroboros.storage.runtime_models import TaskRequest

    store.tasks_repo.create_task(TaskRequest(id="task-1", detached=True, scope=TEST_SCOPE.to_dict()))
    claim = store.tasks_repo.claim_next_task(runtime_id="runtime")
    emit_task_results(
        SimpleNamespace(drive_root=drive_root),
        None,
        [],
        claim.to_agent_input(),
        "done",
        {},
        {},
        time.time(),
        drive_root,
        ctx=ctx,
        scope=TEST_SCOPE,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    assert store.tasks_repo.get_task("task-1").metadata["terminal_attachments"] == ctx.terminal_attachments


@parametrize_from_dict({"False": {"wired": False}, "True": {"wired": True}})
def test_unavailable_or_raising_outbound_reports_failure(tmp_path, wired):
    from tests.helpers.outbound import RaisingOutbound

    ctx = _ctx(tmp_path, outbound=RaisingOutbound() if wired else None)
    report = ctx.task_workspace / "report.txt"
    report.write_text("report")
    result = _tool("send_file")(ctx, file_path=str(report))
    assert result.startswith("⚠️")
    assert wired or "Attachment transport is unavailable" in result


@parametrize_from_dict({"send_file": {"member": "send_file"}, "send_image": {"member": "send_image"}})
def test_the_bridge_satisfies_the_boundary_it_is_handed_over_as(member: str) -> None:
    from supervisor.message_bus import LocalChatBridge

    assert issubclass(LocalChatBridge, ToolOutbound) and hasattr(LocalChatBridge, member)
