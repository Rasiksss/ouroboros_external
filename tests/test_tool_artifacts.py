from pathlib import Path


def test_default_large_tool_output_omits_raw_body_and_artifact(tmp_path: Path):
    from ouroboros.agent.tool_receipts import receipt_for_tool_result

    receipt = receipt_for_tool_result(
        drive_root=tmp_path,
        task_id="task1",
        tool_call_id="call1",
        tool_name="skill_exec",
        tool_args={"description": "demo"},
        result="HEAD" + ("x" * 50_000) + "TAIL secret_token=sk-abcdef123456",
        spill_threshold=10_000,
    )

    artifact = tmp_path / "runtime" / "tool_artifacts" / "tmp" / "task1" / "call1.txt"
    assert not artifact.exists()
    assert "artifact_ref=" not in receipt
    assert "artifact_path=" not in receipt
    assert "sha256=" in receipt
    assert "full result omitted" in receipt
    assert "sk-abcdef123456" not in receipt
    assert "HEAD" not in receipt
    assert "TAIL" not in receipt


def test_local_tmp_debug_writes_file_but_never_exposes_prompt_path(tmp_path: Path, monkeypatch):
    from ouroboros.agent.tool_receipts import receipt_for_tool_result

    def _explode(*args, **kwargs):
        raise AssertionError("local_tmp artifact must not use durable storage")

    monkeypatch.setenv("OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED", "true")
    monkeypatch.setattr("ouroboros.storage.blob.drive.get_drive_storage", _explode)

    receipt = receipt_for_tool_result(
        drive_root=tmp_path,
        task_id="task1",
        tool_call_id="call1",
        tool_name="skill_exec",
        tool_args={"skill": "demo"},
        result="HEAD" + ("x" * 50_000) + "TAIL",
        spill_threshold=10_000,
    )

    artifact = tmp_path / "runtime" / "tool_artifacts" / "tmp" / "task1" / "call1.txt"
    assert artifact.exists()
    assert "artifact_ref=" not in receipt
    assert "artifact_path=" not in receipt
    assert "HEAD" in artifact.read_text(encoding="utf-8")
    assert "TAIL" in artifact.read_text(encoding="utf-8")
    assert "TAIL" not in receipt


def test_durable_artifact_path_is_readable_via_data_read_file_backend(tmp_path: Path, monkeypatch):
    from tests.conftest import refresh_test_config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    refresh_test_config()

    from new_core.tools.context import ToolContext
    from ouroboros.agent.tool_receipts import receipt_for_tool_result
    from ouroboros.storage.blob.keys import parse_data_rel_path
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
    from ouroboros.tools.core import _data_read

    receipt = receipt_for_tool_result(
        drive_root=data_dir,
        task_id="task1",
        tool_call_id="call1",
        tool_name="skill_exec",
        tool_args={"skill": "demo"},
        result="durable HEAD" + ("x" * 50_000) + "durable TAIL",
        spill_threshold=10_000,
        promote_to_durable=True,
        durable_reason="runtime_policy",
    )

    artifact_path = "uploads/task_artifacts/task1/call1.txt"
    assert f"artifact_path={artifact_path}" in receipt
    assert parse_data_rel_path(artifact_path) == (SCOPE_UPLOADS, "task_artifacts/task1/call1.txt")
    assert not (data_dir / "runtime" / "task_artifacts" / "task1" / "call1.txt").exists()
    ctx = ToolContext(repo_dir=data_dir, drive_root=data_dir, task_id="task1", current_chat_id=1)

    read_result = _data_read(ctx, artifact_path, max_lines=10)

    assert "durable HEAD" in read_result
    assert "durable TAIL" in read_result


def test_receipt_free_text_args_are_hash_only(tmp_path: Path):
    from ouroboros.agent.tool_receipts import receipt_for_tool_result

    receipt = receipt_for_tool_result(
        drive_root=tmp_path,
        task_id="task1",
        tool_call_id="call1",
        tool_name="run_shell",
        tool_args={
            "path": "memory/safe.md",
            "content": "secret raw body",
            "description": "private user request",
            "query": "sensitive search terms",
            "command": "echo secret raw body",
            "script": "print('secret raw body')",
        },
        result="ok",
        spill_threshold=10_000,
    )

    assert "memory/safe.md" in receipt
    assert "secret raw body" not in receipt
    assert "private user request" not in receipt
    assert "sensitive search terms" not in receipt
    assert "content_len" in receipt
    assert "command_sha256" in receipt
    assert "script_sha256" in receipt


def test_tmp_artifact_cleanup_keeps_promoted_artifacts(tmp_path: Path):
    from ouroboros.agent.tool_receipts import cleanup_tmp_tool_artifacts

    tmp_artifact = tmp_path / "runtime" / "tool_artifacts" / "tmp" / "task1" / "call1.txt"
    promoted = tmp_path / "uploads" / "task_artifacts" / "task1" / "call1.txt"
    tmp_artifact.parent.mkdir(parents=True)
    promoted.parent.mkdir(parents=True)
    tmp_artifact.write_text("tmp", encoding="utf-8")
    promoted.write_text("promoted", encoding="utf-8")

    deleted = cleanup_tmp_tool_artifacts(tmp_path, task_id="task1")

    assert deleted == 1
    assert not tmp_artifact.exists()
    assert promoted.exists()
