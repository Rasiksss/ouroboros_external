from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from ouroboros.egress.workstation.bridge import FileStat
from ouroboros.egress.workstation.client import WorkstationAccessDenied, WorkstationConfig
from ouroboros.egress.workstation.executor import ExecOutcome
from ouroboros.tools.workstation import apply_patch_tool, edit_tool, exec_tool, read_tool, write_tool
from ouroboros.tools.workstation.connect_tool import _handle_workstation_connect
from tests.helpers.files import stored_file


def _make_ctx(workstation: Any = None) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.workstation = workstation
    return ctx


def _handle(**overrides: Any) -> Any:
    handle = MagicMock()
    handle.config.workspace_root = "/workspace"
    handle.config.access_level = "rw"
    for key, value in overrides.items():
        setattr(handle, key, value)
    return handle


# --- no-session guard, shared across all five ------------------------------------


@pytest.mark.parametrize(
    "module,handler_name,kwargs",
    [
        (exec_tool, "_handle_code_exec", {"command": "echo hi"}),
        (read_tool, "_handle_file_read", {"path": "a.txt"}),
        (write_tool, "_handle_file_write", {"path": "a.txt", "content": "x"}),
        (edit_tool, "_handle_file_edit", {"path": "a.txt", "edits": [{"oldText": "a", "newText": "b"}]}),
        (apply_patch_tool, "_handle_apply_patch", {"input": "*** Begin Patch\n*** End Patch"}),
    ],
)
def test_no_session_returns_tool_unavailable(module: Any, handler_name: str, kwargs: dict[str, Any]) -> None:
    ctx = _make_ctx(workstation=None)
    handler = getattr(module, handler_name)
    result = handler(ctx, **kwargs)
    assert result.startswith("⚠️ TOOL_UNAVAILABLE")


# --- code_exec --------------------------------------------------------------------


def test_code_exec_happy_path() -> None:
    handle = _handle()
    handle.commands.run_streamed.return_value = ExecOutcome(
        exit_code=0, stdout="hi", stderr="", timed_out=False, stdout_overflowed=False, stderr_overflowed=False
    )
    ctx = _make_ctx(workstation=handle)

    result = exec_tool._handle_code_exec(ctx, command="echo hi")

    assert "hi" in result
    assert "exit_code=0" in result
    handle.commands.run_streamed.assert_called_once()
    _, call_kwargs = handle.commands.run_streamed.call_args
    assert call_kwargs["workdir"] == "/workspace"


def test_code_exec_requires_command() -> None:
    ctx = _make_ctx(workstation=_handle())
    result = exec_tool._handle_code_exec(ctx, command="")
    assert "CODE_EXEC_ERROR" in result


def test_code_exec_not_restricted_by_ro_access_level() -> None:
    handle = _handle()
    handle.config.access_level = "ro"
    handle.commands.run_streamed.return_value = ExecOutcome(
        exit_code=0, stdout="", stderr="", timed_out=False, stdout_overflowed=False, stderr_overflowed=False
    )
    ctx = _make_ctx(workstation=handle)

    result = exec_tool._handle_code_exec(ctx, command="echo hi")

    assert "(no output)" in result
    handle.commands.run_streamed.assert_called_once()


def test_code_exec_empty_output_renders_placeholder() -> None:
    handle = _handle()
    handle.commands.run_streamed.return_value = ExecOutcome(
        exit_code=0, stdout="", stderr="", timed_out=False, stdout_overflowed=False, stderr_overflowed=False
    )
    ctx = _make_ctx(workstation=handle)

    result = exec_tool._handle_code_exec(ctx, command="true")

    assert "(no output)" in result


def test_code_exec_rejects_invalid_workdir() -> None:
    ctx = _make_ctx(workstation=_handle())
    result = exec_tool._handle_code_exec(ctx, command="ls", workdir="/etc/passwd")
    assert "CODE_EXEC_ERROR" in result


# --- file_read ---------------------------------------------------------------------


def test_file_read_not_found() -> None:
    handle = _handle()
    handle.files.stat.return_value = None
    ctx = _make_ctx(workstation=handle)

    result = read_tool._handle_file_read(ctx, path="missing.txt")

    assert "not found" in result


def test_file_read_directory_returns_listing() -> None:
    handle = _handle()
    handle.files.stat.return_value = FileStat(path="/workspace/dir", kind="dir")
    handle.files.readdir.return_value = {
        "path": "/workspace/dir",
        "children": [{"path": "/workspace/dir/a.txt", "kind": "file"}],
    }
    ctx = _make_ctx(workstation=handle)

    result = read_tool._handle_file_read(ctx, path="dir")

    assert "directory listing" in result
    assert "a.txt" in result


def test_file_read_delivers_text_via_send_file(tmp_path) -> None:
    handle = _handle()
    handle.files.stat.return_value = FileStat(path="/workspace/a.txt", kind="file", bytes=5)
    handle.files.read_file.return_value = b"hello"
    ctx = _make_ctx(workstation=handle)
    ctx.task_workspace = tmp_path

    with patch("ouroboros.tools.service.messaging._send_file", return_value="OK: delivered") as mock_send:
        result = read_tool._handle_file_read(ctx, path="a.txt")

    assert "OK" in result
    assert mock_send.called


def test_file_read_delivers_image_via_send_image(tmp_path) -> None:
    handle = _handle()
    handle.files.stat.return_value = FileStat(path="/workspace/a.png", kind="file")
    handle.files.read_file.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ctx = _make_ctx(workstation=handle)
    ctx.task_workspace = tmp_path

    with patch("ouroboros.tools.service.messaging._send_image", return_value="OK: delivered") as mock_send:
        result = read_tool._handle_file_read(ctx, path="a.png")

    assert "OK" in result
    assert mock_send.called


def test_file_read_falls_back_to_text_without_workspace() -> None:
    handle = _handle()
    handle.files.stat.return_value = FileStat(path="/workspace/a.txt", kind="file", bytes=5)
    handle.files.read_file.return_value = b"hello"
    ctx = _make_ctx(workstation=handle)
    ctx.task_workspace = None

    result = read_tool._handle_file_read(ctx, path="a.txt")

    assert "hello" in result


# --- file_write ---------------------------------------------------------------------


def test_file_write_happy_path() -> None:
    handle = _handle()
    ctx = _make_ctx(workstation=handle)

    result = write_tool._handle_file_write(ctx, path="a.txt", content="hello")

    assert "OK" in result
    handle.files.write_file.assert_called_once()


def test_file_write_refuses_when_read_only() -> None:
    handle = _handle()
    handle.files.write_file.side_effect = WorkstationAccessDenied("workspace access level is 'ro'")
    ctx = _make_ctx(workstation=handle)

    result = write_tool._handle_file_write(ctx, path="a.txt", content="hello")

    assert "ACCESS_DENIED" in result


# --- file_edit ---------------------------------------------------------------------


def test_file_edit_happy_path() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"hello world"
    ctx = _make_ctx(workstation=handle)

    result = edit_tool._handle_file_edit(ctx, path="a.txt", edits=[{"oldText": "world", "newText": "there"}])

    assert "OK" in result
    handle.files.write_file.assert_called_once()
    args, _ = handle.files.write_file.call_args
    assert args[1] == b"hello there"


def test_file_edit_miss_returns_file_content_as_hint() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"hello world"
    ctx = _make_ctx(workstation=handle)

    result = edit_tool._handle_file_edit(ctx, path="a.txt", edits=[{"oldText": "nope", "newText": "x"}])

    assert "FILE_EDIT_NOT_FOUND" in result
    assert "hello world" in result
    handle.files.write_file.assert_not_called()


def test_file_edit_overlapping_edits_rejected() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"abcdef"
    ctx = _make_ctx(workstation=handle)

    result = edit_tool._handle_file_edit(
        ctx, path="a.txt", edits=[{"oldText": "abcd", "newText": "X"}, {"oldText": "cdef", "newText": "Y"}]
    )

    assert "FILE_EDIT_NOT_FOUND" in result
    handle.files.write_file.assert_not_called()


def test_file_edit_lenient_typographic_match() -> None:
    handle = _handle()
    handle.files.read_file.return_value = "curly ‘quote’ here".encode()
    ctx = _make_ctx(workstation=handle)

    result = edit_tool._handle_file_edit(ctx, path="a.txt", edits=[{"oldText": "'quote'", "newText": "X"}])

    assert "OK" in result
    args, _ = handle.files.write_file.call_args
    assert args[1] == b"curly X here"


def test_file_edit_refuses_when_read_only() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"hello world"
    handle.files.write_file.side_effect = WorkstationAccessDenied("ro")
    ctx = _make_ctx(workstation=handle)

    result = edit_tool._handle_file_edit(ctx, path="a.txt", edits=[{"oldText": "world", "newText": "there"}])

    assert "ACCESS_DENIED" in result


# --- apply_patch ---------------------------------------------------------------------


def test_apply_patch_add_file() -> None:
    handle = _handle()
    ctx = _make_ctx(workstation=handle)
    patch = "*** Begin Patch\n*** Add File: new.txt\n+line one\n+line two\n*** End Patch"

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "1 added" in result
    handle.files.write_file.assert_called_once()
    args, _ = handle.files.write_file.call_args
    assert args[0] == "new.txt"
    assert args[1] == b"line one\nline two"


def test_apply_patch_delete_file() -> None:
    handle = _handle()
    ctx = _make_ctx(workstation=handle)
    patch = "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch"

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "1 deleted" in result
    handle.files.remove.assert_called_once_with("gone.txt")


def test_apply_patch_update_file_with_hunk() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"line1\nline2\nline3\n"
    ctx = _make_ctx(workstation=handle)
    patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n line1\n-line2\n+line2-changed\n line3\n*** End Patch"

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "1 updated" in result
    args, _ = handle.files.write_file.call_args
    assert args[0] == "a.txt"
    assert args[1] == b"line1\nline2-changed\nline3\n"


def test_apply_patch_update_with_move() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"content\n"
    ctx = _make_ctx(workstation=handle)
    patch = "*** Begin Patch\n*** Update File: old.txt\n*** Move to: new.txt\n@@\n-content\n+content2\n*** End Patch"

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "1 updated" in result
    write_args, _ = handle.files.write_file.call_args
    assert write_args[0] == "new.txt"
    handle.files.remove.assert_called_once_with("old.txt")


def test_apply_patch_hunk_context_mismatch_fails_whole_call_no_writes() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"unrelated content\n"
    ctx = _make_ctx(workstation=handle)
    patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-missing line\n+replacement\n*** End Patch"

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "APPLY_PATCH_FAILED" in result
    handle.files.write_file.assert_not_called()
    handle.files.remove.assert_not_called()


def test_apply_patch_malformed_input_is_parse_error() -> None:
    ctx = _make_ctx(workstation=_handle())
    result = apply_patch_tool._handle_apply_patch(ctx, input="not a patch")
    assert "APPLY_PATCH_PARSE_ERROR" in result


def test_apply_patch_multi_file_all_or_nothing() -> None:
    handle = _handle()
    handle.files.read_file.return_value = b"has this text\n"
    ctx = _make_ctx(workstation=handle)
    patch = (
        "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** Update File: a.txt\n@@\n-does not exist\n+x\n*** End Patch"
    )

    result = apply_patch_tool._handle_apply_patch(ctx, input=patch)

    assert "APPLY_PATCH_FAILED" in result
    # The Add op must not have been applied either, since validation for the
    # whole patch happens before any write.
    handle.files.write_file.assert_not_called()


# --- workstation_connect ---------------------------------------------------------


def _make_ws_ctx(
    workstation: Any = None, chat_id: str = "chat-42", owner_id: str = "owner-1", agent_id: str = "agent-1"
) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.workstation = workstation
    # A real scope, not a mock: the attachment tests store and read back under it, and
    # the storage keys are built from its axes.
    ctx.scope = OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id="browser", chat_id=chat_id)
    ctx.task_id = "workstation-task"
    ctx.attachments = []
    return ctx


def test_connect_already_active_returns_noop() -> None:
    handle = _handle()
    handle.record.session_id = "sess-123"
    ctx = _make_ws_ctx(workstation=handle)

    result = _handle_workstation_connect(ctx)

    assert "already active" in result
    assert "sess-123" in result


@patch("ouroboros.egress.workstation.workstation_manager")
def test_connect_happy_path(mock_manager: MagicMock) -> None:
    handle = _handle()
    mock_manager.get_or_create.return_value = handle
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = "https://ws.test"
        mock_settings.return_value = settings

        with patch("ouroboros.egress.workstation.client.WorkstationConfig") as mock_cfg_cls:
            mock_cfg_cls.from_settings.return_value = WorkstationConfig(base_url="https://ws.test", create_secret="s")

            result = _handle_workstation_connect(ctx)

    assert "OK" in result
    assert ctx.workstation is handle
    mock_manager.get_or_create.assert_called_once()


@patch("ouroboros.egress.workstation.workstation_manager")
def test_connect_sets_ctx_workstation(mock_manager: MagicMock) -> None:
    handle = _handle()
    mock_manager.get_or_create.return_value = handle
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = "https://ws.test"
        mock_settings.return_value = settings

        with patch("ouroboros.egress.workstation.client.WorkstationConfig") as mock_cfg_cls:
            mock_cfg_cls.from_settings.return_value = WorkstationConfig(base_url="https://ws.test", create_secret="s")
            _handle_workstation_connect(ctx)

    assert ctx.workstation is handle


def test_connect_no_base_url_returns_unavailable() -> None:
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = ""
        mock_settings.return_value = settings

        result = _handle_workstation_connect(ctx)

    assert "WORKSTATION_UNAVAILABLE" in result
    assert ctx.workstation is None


def test_connect_config_none_returns_unavailable() -> None:
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = "https://ws.test"
        mock_settings.return_value = settings

        with patch("ouroboros.egress.workstation.client.WorkstationConfig") as mock_cfg_cls:
            mock_cfg_cls.from_settings.return_value = None
            result = _handle_workstation_connect(ctx)

    assert "WORKSTATION_UNAVAILABLE" in result
    assert ctx.workstation is None


@patch("ouroboros.egress.workstation.workstation_manager")
def test_connect_manager_exception_returns_unavailable(mock_manager: MagicMock) -> None:
    mock_manager.get_or_create.side_effect = RuntimeError("service down")
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = "https://ws.test"
        mock_settings.return_value = settings

        with patch("ouroboros.egress.workstation.client.WorkstationConfig") as mock_cfg_cls:
            mock_cfg_cls.from_settings.return_value = WorkstationConfig(base_url="https://ws.test", create_secret="s")
            result = _handle_workstation_connect(ctx)

    assert "WORKSTATION_UNAVAILABLE" in result
    assert ctx.workstation is None


@patch("ouroboros.egress.workstation.attachments_sync.sync_turn_attachments")
@patch("ouroboros.egress.workstation.workstation_manager")
def test_connect_attachment_sync_error_does_not_break_handler(mock_manager: MagicMock, mock_sync: MagicMock) -> None:
    handle = _handle()
    mock_manager.get_or_create.return_value = handle
    mock_sync.side_effect = RuntimeError("sync failed")
    ctx = _make_ws_ctx()

    with patch("ouroboros.storage.settings.store.load_settings") as mock_settings:
        settings = MagicMock()
        settings.WORKSTATION_BASE_URL = "https://ws.test"
        mock_settings.return_value = settings

        with patch("ouroboros.egress.workstation.client.WorkstationConfig") as mock_cfg_cls:
            mock_cfg_cls.from_settings.return_value = WorkstationConfig(base_url="https://ws.test", create_secret="s")
            result = _handle_workstation_connect(ctx)

    assert "OK" in result
    assert ctx.workstation is handle


# --- file_write ----------------------------------------------------------------


def test_file_write_writes_inline_content() -> None:
    handle = _handle()
    ctx = _make_ws_ctx(workstation=handle, chat_id="chat-42")

    result = write_tool._handle_file_write(ctx, path="a.txt", content="hello")

    assert "OK" in result
    handle.files.write_file.assert_called_once_with("a.txt", b"hello", mime="text/plain; charset=utf-8")


def test_file_write_data_path_requires_uploads_prefix(tmp_path) -> None:
    ctx = _make_ws_ctx(workstation=_handle(), chat_id="chat-42")
    ctx.drive_path = lambda rel: (tmp_path / rel).resolve()

    result = write_tool._handle_file_write(ctx, path="a.txt", data_path="scratch/a.txt")

    assert "FILE_WRITE_ERROR" in result
    assert "uploads" in result


def test_file_write_data_path_not_found(tmp_path) -> None:
    ctx = _make_ws_ctx(workstation=_handle(), chat_id="chat-42")
    ctx.drive_root = tmp_path / "data"
    ctx.drive_path = lambda rel: (ctx.drive_root / rel).resolve()

    result = write_tool._handle_file_write(ctx, path="a.txt", data_path="uploads/missing.txt")

    assert "FILE_WRITE_ERROR" in result
    assert "not found" in result


@pytest.mark.parametrize("lowered_limit", [False, True])
@pytest.mark.parametrize("reference", ["chat/attachment-1", "chat/attachment-1/img.png", "img.png"])
def test_file_write_data_path_respects_stored_attachment_limit(
    tmp_path, isolate_settings, lowered_limit, reference
) -> None:
    handle = _handle()
    drive_root = tmp_path / "data"
    ctx = _make_ws_ctx(workstation=handle, chat_id="chat-42")
    ctx.drive_root = drive_root
    ctx.drive_path = lambda rel: (drive_root / rel).resolve()

    from ouroboros.storage.blob.factory import get_storage_provider

    provider = get_storage_provider()
    metadata = stored_file(
        scope=ctx.scope,
        provider=provider,
        data=b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
        attachment_id="attachment-1",
        filename="img.png",
        mime="image/png",
        source="browser",
    )
    from tests.helpers.files import link_file

    link_file(metadata, scope=ctx.scope)
    if lowered_limit:
        isolate_settings.save_settings({"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 8})

    if reference == "img.png":
        upload = drive_root / "uploads" / reference
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    result = write_tool._handle_file_write(ctx, path="inbound/img.png", data_path=f"uploads/{reference}")

    if lowered_limit:
        assert result.startswith("⚠️ FILE_WRITE_ERROR:")
        assert "8 bytes" in result
        handle.files.write_file.assert_not_called()
        return
    assert "OK" in result
    handle.files.write_file.assert_called_once()
    args, kwargs = handle.files.write_file.call_args
    assert args[0] == "inbound/img.png"
    assert args[1] == b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    assert kwargs["mime"] == "image/png"


def test_file_write_data_path_chat_id_with_filename(tmp_path) -> None:
    handle = _handle()
    drive_root = tmp_path / "data"
    ctx = _make_ws_ctx(workstation=handle, chat_id="chat-42")
    ctx.drive_root = drive_root
    ctx.drive_path = lambda rel: (drive_root / rel).resolve()

    from ouroboros.storage.blob.factory import get_storage_provider

    provider = get_storage_provider()
    metadata = stored_file(
        scope=ctx.scope,
        provider=provider,
        data=b"hello",
        attachment_id="attachment-2",
        filename="notes.txt",
        mime="text/plain",
        source="browser",
    )
    from tests.helpers.files import link_file

    link_file(metadata, scope=ctx.scope)

    result = write_tool._handle_file_write(ctx, path="copy/notes.txt", data_path="uploads/chat/attachment-2/notes.txt")

    assert "OK" in result
    args, kwargs = handle.files.write_file.call_args
    assert args[1] == b"hello"
    assert kwargs["mime"] == "text/plain"


def test_file_write_data_path_refuses_unknown_file(tmp_path) -> None:
    handle = _handle()
    drive_root = tmp_path / "data"
    ctx = _make_ws_ctx(workstation=handle, chat_id="chat-42")
    ctx.drive_root = drive_root
    ctx.drive_path = lambda rel: (drive_root / rel).resolve()

    result = write_tool._handle_file_write(ctx, path="a.txt", data_path="uploads/chat/unknown-attachment")

    assert "FILE_WRITE_ERROR" in result
    assert "not found" in result


def test_file_write_data_path_resolves_by_filename_from_blob(tmp_path) -> None:
    handle = _handle()
    drive_root = tmp_path / "data"
    ctx = _make_ws_ctx(workstation=handle, chat_id="chat-42")
    ctx.drive_root = drive_root
    ctx.drive_path = lambda rel: (drive_root / rel).resolve()

    from ouroboros.storage.blob.factory import get_storage_provider

    provider = get_storage_provider()
    metadata = stored_file(
        scope=ctx.scope,
        provider=provider,
        data=b"\x89PNG\r\n\x1a\n" + b"\xaa" * 4,
        attachment_id="attachment-3",
        filename="report.png",
        mime="image/png",
        source="browser",
    )
    from tests.helpers.files import link_file

    link_file(metadata, scope=ctx.scope)

    result = write_tool._handle_file_write(ctx, path="inbound/report.png", data_path="uploads/report.png")

    assert "OK" in result
    args, kwargs = handle.files.write_file.call_args
    assert args[1] == b"\x89PNG\r\n\x1a\n" + b"\xaa" * 4
    assert kwargs["mime"] == "image/png"


def test_file_write_content_and_data_path_mutually_exclusive(tmp_path) -> None:
    ctx = _make_ws_ctx(workstation=_handle(), chat_id="chat-42")
    ctx.drive_path = lambda rel: (tmp_path / rel).resolve()

    result = write_tool._handle_file_write(ctx, path="a.txt", content="hello", data_path="uploads/a.txt")

    assert "FILE_WRITE_ERROR" in result
    assert "mutually exclusive" in result
