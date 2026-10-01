"""Tests for the block-based memory pipeline.

Scratchpad blocks moved to the runtime DB — their storage rules are covered by
``test_scratchpad_domain.py`` and ``test_scratchpad_db.py``. What remains here
is the end-to-end shape the agent sees.
"""

import pytest

from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.settings.paths import load_path_settings


@pytest.fixture
def memory_dir():
    """The one directory startup may write to: ``{DATA_DIR}/memory``."""
    return load_path_settings().data_dir / "memory"


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    """Drop the cached services so this test binds to its own runtime DB."""
    get_scratchpad.cache_clear()
    get_memory.cache_clear()


class TestScratchpadBlocks:
    def test_load_empty_blocks(self):
        assert get_scratchpad().list_blocks() == []

    def test_append_block(self):
        block = get_scratchpad().append_block("test content", source="test")
        assert block["content"] == "test content"
        assert block["source"] == "test"
        assert len(get_scratchpad().list_blocks()) == 1

    def test_fifo_rotation(self):
        scratchpad = get_scratchpad()
        for i in range(SCRATCHPAD_MAX_BLOCKS + 3):
            scratchpad.append_block(f"block {i}")
        blocks = scratchpad.list_blocks()
        assert len(blocks) == SCRATCHPAD_MAX_BLOCKS
        assert "block 3" in blocks[0].content

    def test_rendered_view(self):
        scratchpad = get_scratchpad()
        scratchpad.append_block("first block")
        scratchpad.append_block("second block")
        md = scratchpad.render()
        assert "second block" in md
        assert "first block" in md
        assert f"{SCRATCHPAD_MAX_BLOCKS} blocks" in md

    def test_append_defaults_to_the_task_source(self):
        get_scratchpad().append_block("new block")
        blocks = get_scratchpad().list_blocks()
        assert len(blocks) == 1
        assert blocks[0].source == "task"

    def test_startup_writes_no_memory_files(self, memory_dir):
        """Startup writes no blobs at all now: identity seeds as a row."""
        get_memory().load_identity()
        assert not (memory_dir / "identity.md").exists()
        assert not (memory_dir / "scratchpad.md").exists()
        assert not (memory_dir / "scratchpad_blocks.json").exists()
        assert not (memory_dir / "scratchpad_journal.jsonl").exists()

    def test_eviction_writes_no_journal_file(self, memory_dir):
        scratchpad = get_scratchpad()
        for i in range(SCRATCHPAD_MAX_BLOCKS + 2):
            scratchpad.append_block(f"block {i}")
        assert not (memory_dir / "scratchpad_journal.jsonl").exists()

    def test_startup_writes_no_dialogue_files(self, memory_dir):
        get_memory().load_identity()
        assert not (memory_dir / "dialogue_blocks.json").exists()
        assert not (memory_dir / "dialogue_summary.md").exists()
        assert not (memory_dir / "dialogue_meta.json").exists()
