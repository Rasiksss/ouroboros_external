from dataclasses import replace

from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.core import _data_list, _data_read, _data_write
from ouroboros.tools.service.artifacts import materialize, provenance_path, publish, resolve_artifact
from ouroboros.tools.service.messaging import _send_file
from tests.helpers.files import outbound_bytes
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, scope_with


@parametrize_from_dict({"task fallback": {"flat": False}, "exact flat path": {"flat": True}})
def test_short_artifact_reference_prefers_exact_path_then_current_task(tmp_path, flat):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "report.txt"
    first = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path / "data", task_id="A")
    path.write_text("first version")
    first_reference = publish(first, first.scope, workspace, [path])[0]
    first_id = resolve_artifact(first, first_reference)["id"]
    second = replace(first, task_id="B")
    path.write_text("second version")
    second_reference = publish(second, second.scope, workspace, [path])[0]
    if flat:
        assert _data_write(second, "uploads/report.txt", "flat version").startswith("OK")
    destination = tmp_path / "materialized"
    destination.mkdir()
    result, error = materialize(second, "uploads/report.txt", destination)
    assert not error
    assert result.read_text() == ("flat version" if flat else "second version")
    assert resolve_artifact(second, first_id)["id"] == first_id
    assert resolve_artifact(second, first_reference)["id"] == first_id
    assert "second version" in _data_read(second, second_reference)


def test_later_task_reads_lists_appends_and_sends_latest_chat_artifact(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path / "data", task_id="A")
    reference = "uploads/report.txt"
    assert _data_write(first, reference, "old").startswith("OK")
    assert _data_write(first, reference, "original").startswith("OK")
    old = resolve_artifact(first, reference)
    second = replace(first, task_id="B", task_workspace=workspace, outbound=RecordingOutbound())
    assert "original" in _data_read(second, reference)
    assert "report.txt" in _data_list(second, "uploads")
    assert _data_write(second, reference, " + appended", mode="append").startswith("OK")
    third = replace(second, task_id="C")
    assert "original + appended" in _data_read(third, reference)
    assert _data_list(third, "uploads").count("report.txt") == 1
    assert _send_file(third, reference).startswith("OK:")
    assert outbound_bytes(third.outbound.files[0][0]) == b"original + appended"
    assert "original" in _data_read(first, f"uploads/{old['storage_key']}")
    assert resolve_artifact(third, reference)["id"] != old["id"]


@parametrize_from_dict(
    {
        "chat": {"axis": "chat_id"},
        "surface": {"axis": "surface_id"},
        "owner": {"axis": "owner_id"},
        "agent": {"axis": "agent_id"},
    }
)
def test_generated_artifact_is_not_implicitly_shared_outside_its_chat(tmp_path, axis):
    first = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path / "data", task_id="A")
    assert _data_write(first, "uploads/report.txt", "private").startswith("OK")
    file = resolve_artifact(first, "uploads/report.txt")
    other = replace(
        first, task_id="B", scope=scope_with(TEST_SCOPE, **{axis: "a2a" if axis == "surface_id" else "other"})
    )
    assert resolve_artifact(other, "uploads/report.txt") is None
    assert "report.txt" not in _data_list(other, "uploads")
    if axis in {"chat_id", "surface_id"}:
        assert resolve_artifact(replace(other, attachments=[file]), file["id"]) is not None


@parametrize_from_dict(
    {
        "changed with proof": {"changed": True, "known_fingerprint": True},
        "unchanged": {"changed": False, "known_fingerprint": True},
        "changed without proof": {"changed": True, "known_fingerprint": False},
    }
)
def test_deleted_workspace_source_requires_proof_of_changed_bytes(tmp_path, changed, known_fingerprint):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path / "data",
        task_id="A",
        task_workspace=workspace,
        outbound=RecordingOutbound(),
    )
    assert _data_write(ctx, "uploads/report.txt", "original").startswith("OK")
    file = resolve_artifact(ctx, "uploads/report.txt")
    local, error = materialize(ctx, "uploads/report.txt", workspace)
    assert not error
    if not known_fingerprint:
        provenance_path(local).write_text('{"file_id": "' + file["id"] + '"}')
    with runtime_persistence.current.engine.begin() as conn:
        FileCatalog(conn, ctx.scope).delete_file(file["id"])
    if changed:
        local.write_text("edited")
    result = _send_file(ctx, str(local))
    if changed and known_fingerprint:
        assert result.startswith("OK:"), result
        assert outbound_bytes(ctx.outbound.files[0][0]) == b"edited"
    else:
        assert "unavailable" in result
        assert not ctx.outbound.files
