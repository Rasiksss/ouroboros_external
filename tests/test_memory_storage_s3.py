"""Memory and knowledge persistence under S3 backend (strict, no local shadow reads)."""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("moto")

from moto import mock_aws


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    yield
    reset_storage_provider_for_tests()


def _setup_s3(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    import boto3

    from tests.conftest import refresh_test_config

    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    bucket = "ouroboros-memory-test"
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OUROBOROS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("OUROBOROS_S3_BUCKET", bucket)
    monkeypatch.setenv("OUROBOROS_S3_REGION", "us-east-1")
    refresh_test_config()
    from ouroboros.storage.blob.factory import reset_storage_provider_for_tests

    reset_storage_provider_for_tests()
    return data_dir


@mock_aws
def test_path_exists_strict_ignores_local_shadow(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_s3(monkeypatch, tmp_path)
    identity = data_dir / "memory" / "identity.md"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text("local only", encoding="utf-8")

    from ouroboros.storage.blob.drive import get_drive_storage

    drive = get_drive_storage(data_dir)
    assert drive.path_exists(identity) is False


@mock_aws
def test_durable_tool_artifact_written_and_read_via_s3(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = _setup_s3(monkeypatch, tmp_path)
    from new_core.tools.context import ToolContext
    from ouroboros.agent.tool_receipts import receipt_for_tool_result
    from ouroboros.storage.blob.drive import get_drive_storage
    from ouroboros.tools.core import _data_read

    receipt = receipt_for_tool_result(
        drive_root=data_dir,
        task_id="task1",
        tool_call_id="call1",
        tool_name="skill_exec",
        tool_args={"skill": "demo"},
        result="durable HEAD\n" + ("x" * 50_000) + "\ndurable TAIL",
        spill_threshold=10_000,
        promote_to_durable=True,
        durable_reason="runtime_policy",
    )

    artifact_path = "uploads/task_artifacts/task1/call1.txt"
    artifact_file = data_dir / "uploads" / "task_artifacts" / "task1" / "call1.txt"
    assert f"artifact_path={artifact_path}" in receipt
    assert not artifact_file.is_file()

    drive = get_drive_storage(data_dir)
    assert drive.path_exists(artifact_file)

    ctx = ToolContext(repo_dir=data_dir, drive_root=data_dir, task_id="task1", current_chat_id=1)
    read_result = _data_read(ctx, artifact_path, max_lines=10)

    assert "durable HEAD" in read_result
    assert "durable TAIL" in read_result


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached services so this test binds to its own runtime DB and bucket."""
    from ouroboros.storage.core_memory.memory import get_memory
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad.cache_clear()
    get_memory.cache_clear()


@mock_aws
def test_identity_seeds_into_the_runtime_db_and_touches_no_blob(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity is a row: seeding writes neither an object nor a local file."""
    import boto3

    data_dir = _setup_s3(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.memory import get_memory

    seeded = get_memory().load_identity()
    assert "Ouroboros" in seeded

    assert not (data_dir / "memory" / "identity.md").is_file()
    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="ouroboros-memory-test")
    keys = [obj["Key"] for obj in listed.get("Contents", [])]
    assert not [key for key in keys if key.endswith("identity.md")]


@mock_aws
def test_named_facts_go_to_runtime_db_not_s3_blobs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the DB cutover, long-term facts never touch memory/global/* blobs."""
    import boto3

    data_dir = _setup_s3(monkeypatch, tmp_path)
    from ouroboros.agent.context import build_memory_index_sections
    from ouroboros.storage.core_memory.memory import get_memory

    memory = get_memory()

    memory.save("user_role", "user", "Who the user is", "Senior engineer.")

    content, _, _ = memory.read("user_role")
    assert "Senior engineer." in content
    assert "n=user_role" in "\n\n".join(build_memory_index_sections())

    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="ouroboros-memory-test")
    keys = [obj["Key"] for obj in listed.get("Contents", [])]
    assert not [key for key in keys if "memory/global/" in key]
    assert not (data_dir / "memory" / "global").exists()


@mock_aws
def test_knowledge_save_and_list_via_the_memory_tools(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_s3(monkeypatch, tmp_path)
    import boto3

    from new_core.tools.context import ToolContext
    from ouroboros.tools.memory_tools import _memory_list, _memory_save

    ctx = ToolContext(
        repo_dir=data_dir,
        drive_root=data_dir,
        task_id="t1",
        current_chat_id=1,
    )
    result = _memory_save(ctx, "patterns", "knowledge", "Error patterns", "# Patterns\n\nAlways test storage.\n")
    assert "saved" in result.lower()

    assert "patterns" in _memory_list(ctx, memory_type="knowledge").lower()

    local_topic = data_dir / "memory" / "knowledge" / "patterns.md"
    assert not local_topic.is_file()

    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="ouroboros-memory-test")
    keys = [obj["Key"] for obj in listed.get("Contents", [])]
    assert not [key for key in keys if "knowledge" in key]


@mock_aws
def test_no_world_profile_is_ever_written(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WORLD.md is retired: a fresh boot creates no such object on either backend."""
    import boto3

    data_dir = _setup_s3(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.memory import get_memory

    get_memory().load_identity()

    assert not (data_dir / "memory" / "WORLD.md").exists()
    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="ouroboros-memory-test")
    keys = [obj["Key"] for obj in listed.get("Contents", [])]
    assert not [key for key in keys if key.endswith("WORLD.md")]


@mock_aws
def test_scratchpad_blocks_go_to_runtime_db_not_s3_blobs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the DB cutover, scratchpad blocks never touch blob storage."""
    import boto3

    data_dir = _setup_s3(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad().append_block("working note on the s3 backend")

    assert "working note on the s3 backend" in get_scratchpad().render()
    listed = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="ouroboros-memory-test")
    keys = [obj["Key"] for obj in listed.get("Contents", [])]
    assert not [key for key in keys if "scratchpad" in key]
    assert not (data_dir / "memory" / "scratchpad_blocks.json").exists()
    assert not (data_dir / "memory" / "scratchpad_journal.jsonl").exists()


@mock_aws
def test_written_blocks_survive_a_reload_on_the_s3_backend(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the old raw-fd write discarded scratchpad writes on non-file backends."""
    _setup_s3(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    scratchpad = get_scratchpad()
    for i in range(4):
        scratchpad.append_block(f"block {i}")

    get_scratchpad.cache_clear()
    reloaded = [b.content for b in get_scratchpad().list_blocks()]
    assert reloaded == ["block 0", "block 1", "block 2", "block 3"]
