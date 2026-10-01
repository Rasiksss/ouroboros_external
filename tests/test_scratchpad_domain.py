"""Scratchpad domain rules over an in-memory fake port: cap, ordering, render."""

from __future__ import annotations

import pytest

from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.render import render_blocks
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad


class FakeScratchpad(IScratchpad):
    """In-memory ``IScratchpad``: hands out ids the way the database would."""

    def __init__(self) -> None:
        self.rows: list[BlockRow] = []
        self._next_id = 1

    def append_and_evict(self, owner_id: str, ts: str, source: str, content: str, cap: int) -> int:
        self.rows.append(BlockRow(id=self._next_id, owner_id=owner_id, ts=ts, source=source, content=content))
        self._next_id += 1
        active = self.list_active(owner_id)
        over = active[: max(0, len(active) - cap)]
        for row in over:
            row.status = "evicted"
        return len(over)

    def list_active(self, owner_id: str) -> list[BlockRow]:
        active = [r for r in self.rows if r.owner_id == owner_id and r.status == "active"]
        return sorted(active, key=lambda r: r.id)


@pytest.fixture
def scratchpad() -> Scratchpad:
    return Scratchpad(FakeScratchpad())


def test_append_returns_the_written_block(scratchpad):
    block = scratchpad.append_block("test content", source="test")

    assert block["content"] == "test content"
    assert block["source"] == "test"
    assert block["ts"]
    assert len(scratchpad.list_blocks()) == 1


def test_blocks_are_listed_oldest_first_with_increasing_ids(scratchpad):
    for i in range(3):
        scratchpad.append_block(f"block {i}")

    rows = scratchpad.list_blocks()
    assert [r.content for r in rows] == ["block 0", "block 1", "block 2"]
    assert [r.id for r in rows] == sorted(r.id for r in rows)


def test_overflow_evicts_oldest_down_to_the_cap(scratchpad):
    for i in range(SCRATCHPAD_MAX_BLOCKS + 3):
        scratchpad.append_block(f"block {i}")

    rows = scratchpad.list_blocks()
    assert len(rows) == SCRATCHPAD_MAX_BLOCKS
    assert rows[0].content == "block 3"


def test_evicted_blocks_keep_their_content_and_leave_the_view(scratchpad):
    repo = scratchpad._repo
    for i in range(SCRATCHPAD_MAX_BLOCKS + 1):
        scratchpad.append_block(f"block {i}")

    evicted = [r for r in repo.rows if r.status == "evicted"]
    assert [r.content for r in evicted] == ["block 0"]
    assert "block 0" not in scratchpad.render()


def test_block_order_is_the_id_not_the_timestamp(scratchpad):
    """Regression: block order is the id, never the displayed timestamp (D7)."""
    # Written first, but stamped far in the future: id and ts disagree.
    scratchpad._repo.append_and_evict("global", "2030-01-01T00:00:00+00:00", "task", "written first", 100)
    scratchpad.append_block("written second")

    rows = scratchpad.list_blocks()
    assert [r.content for r in rows] == ["written first", "written second"]
    assert rows[0].ts > rows[1].ts
    rendered = scratchpad.render()
    assert rendered.index("written second") < rendered.index("written first")


# ------------------------------------------------------------------ rendering


def _legacy_render(blocks: list[dict]) -> str:
    """The pre-change ``Memory.regenerate_scratchpad_md`` body, verbatim."""
    n = len(blocks)
    parts = [f"## Scratchpad (working memory — {n}/10 blocks)\n"]
    for block in reversed(blocks):
        ts = str(block.get("ts", ""))[:16]
        source = block.get("source", "?")
        content = block.get("content", "")
        parts.append(f"### [{ts} — {source}]\n{content}\n\n---\n")
    return "\n".join(parts)


def test_rendered_view_is_byte_identical_to_the_pre_change_format():
    blocks = [
        {"ts": "2026-07-01T10:00:00+00:00", "source": "task", "content": "first note"},
        {"ts": "2026-07-02T11:30:00+00:00", "source": "consolidation", "content": "second\nnote"},
        {"ts": "2026-07-03T12:45:00+00:00", "source": "task", "content": ""},
    ]
    rows = [
        BlockRow(id=i, owner_id="global", ts=b["ts"], source=b["source"], content=b["content"])
        for i, b in enumerate(blocks, start=1)
    ]

    assert render_blocks(rows) == _legacy_render(blocks)


def test_render_falls_back_to_the_source_placeholder_like_the_old_format():
    """A block stored without a source rendered as '?' before the cutover."""
    row = BlockRow(id=1, owner_id="global", ts="2026-07-01T10:00:00+00:00", source="", content="body")

    assert render_blocks([row]) == _legacy_render([{"ts": row.ts, "content": "body"}])


def test_empty_scratchpad_renders_the_default_text_without_writing(scratchpad):
    rendered = scratchpad.render()

    assert rendered.startswith("# Scratchpad\n\nUpdatedAt: ")
    assert rendered.endswith("\n\n(empty — write anything here)\n")
    assert scratchpad._repo.rows == []
