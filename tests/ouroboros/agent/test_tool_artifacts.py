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

    monkeypatch.setenv("OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED", "true")

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
