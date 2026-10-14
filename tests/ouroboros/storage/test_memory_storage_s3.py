"""Memory and knowledge persistence: the runtime database holds them, object storage holds none."""

from __future__ import annotations

import pathlib

import pytest

from ouroboros.storage.blob.factory import get_storage_provider, reset_storage_provider_for_tests
from ouroboros.storage.blob.scopes import ALL_SCOPES
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx


def _setup_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    from tests.conftest import refresh_test_config

    data_dir = tmp_path / "ouroboros_data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data_dir))
    refresh_test_config()
    reset_storage_provider_for_tests()
    return data_dir


def _stored_keys() -> list[str]:
    provider = get_storage_provider()
    return [f"{scope}/{key}" for scope in sorted(ALL_SCOPES) for key in provider.list_keys(TEST_SCOPE, scope)]


def test_path_exists_does_not_answer_from_the_instance_disk(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = _setup_storage(monkeypatch, tmp_path)
    identity = data_dir / "memory" / "identity.md"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text("local only", encoding="utf-8")

    from ouroboros.storage.blob.drive import get_drive_storage

    drive = get_drive_storage(data_dir)
    assert drive.path_exists(TEST_SCOPE, identity) is False


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached services so this test binds to its own runtime DB."""
    from ouroboros.storage.core_memory.memory import get_memory
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad.cache_clear()
    get_memory.cache_clear()


def test_fresh_memory_is_empty_and_touches_no_blob(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening a fresh store writes no default memory."""
    data_dir = _setup_storage(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.memory import get_memory

    assert get_memory().list_headers(TEST_SCOPE) == []

    assert not (data_dir / "memory" / "identity.md").is_file()
    assert not [key for key in _stored_keys() if key.endswith("identity.md")]


def test_named_facts_go_to_runtime_db_not_blobs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the DB cutover, long-term facts never touch memory/global/* blobs."""
    data_dir = _setup_storage(monkeypatch, tmp_path)
    from ouroboros.agent.context import build_memory_index_sections
    from ouroboros.storage.core_memory.memory import get_memory

    memory = get_memory()

    memory.save(TEST_SCOPE, "user_role", "user", "Who the user is", "Senior engineer.")

    content, _, _ = memory.read(TEST_SCOPE, "user_role")
    assert "Senior engineer." in content
    assert "n=user_role" in "\n\n".join(build_memory_index_sections(TEST_SCOPE))

    assert not [key for key in _stored_keys() if "memory/global/" in key]
    assert not (data_dir / "memory" / "global").exists()


def test_knowledge_save_and_list_via_the_memory_tools(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = _setup_storage(monkeypatch, tmp_path)
    from ouroboros.tools.memory_tools import _memory_list, _memory_save

    ctx = make_tool_ctx(
        repo_dir=data_dir,
        drive_root=data_dir,
        task_id="t1",
    )
    result = _memory_save(ctx, "patterns", "knowledge", "Error patterns", "# Patterns\n\nAlways test storage.\n")
    assert "saved" in result.lower()

    assert "patterns" in _memory_list(ctx, memory_type="knowledge").lower()

    local_topic = data_dir / "memory" / "knowledge" / "patterns.md"
    assert not local_topic.is_file()

    assert not [key for key in _stored_keys() if "knowledge" in key]


def test_no_world_profile_is_ever_written(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WORLD.md is retired: a fresh boot creates no such object."""
    data_dir = _setup_storage(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.memory import get_memory

    assert get_memory().list_headers(TEST_SCOPE) == []

    assert not (data_dir / "memory" / "WORLD.md").exists()
    assert not [key for key in _stored_keys() if key.endswith("WORLD.md")]


def test_scratchpad_blocks_go_to_runtime_db_not_blobs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the DB cutover, scratchpad blocks never touch blob storage."""
    data_dir = _setup_storage(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    get_scratchpad().append_block(TEST_SCOPE, "working note on the shared backend")

    assert "working note on the shared backend" in get_scratchpad().render(TEST_SCOPE)
    assert not [key for key in _stored_keys() if "scratchpad" in key]
    assert not (data_dir / "memory" / "scratchpad_blocks.json").exists()
    assert not (data_dir / "memory" / "scratchpad_journal.jsonl").exists()


def test_written_blocks_survive_a_reload(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the old raw-fd write discarded scratchpad writes on non-file backends."""
    _setup_storage(monkeypatch, tmp_path)
    from ouroboros.storage.core_memory.scratchpad import get_scratchpad

    scratchpad = get_scratchpad()
    for i in range(4):
        scratchpad.append_block(TEST_SCOPE, f"block {i}")

    get_scratchpad.cache_clear()
    reloaded = [b.content for b in get_scratchpad().list_blocks(TEST_SCOPE)]
    assert reloaded == ["block 0", "block 1", "block 2", "block 3"]
