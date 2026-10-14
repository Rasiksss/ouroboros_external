"""Scratchpad domain rules over an in-memory fake port: cap, ordering, render."""

from __future__ import annotations

import pytest

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.render import render_blocks
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad

SCOPE = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1")


class FakeScratchpad(IScratchpad):
    """In-memory ``IScratchpad``: hands out ids the way the database would.

    Blocks are filed under the ownership pair, and every read filters on both axes —
    a fake that keyed on the owner alone would let one agent's appends evict another's
    blocks and no test would notice.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[tuple[str, str, str], BlockRow]] = []
        self._next_id = 1

    @staticmethod
    def _key(scope: OwnerScope, job_id: str = "") -> tuple[str, str, str]:
        return (scope.owner_id, scope.agent_id, job_id)

    def append_and_evict(
        self,
        scope: OwnerScope,
        ts: str,
        source: str,
        content: str,
        cap: int,
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> int:
        self.rows.append((self._key(scope, job_id), BlockRow(id=self._next_id, ts=ts, source=source, content=content)))
        self._next_id += 1
        active = self.list_active(scope, job_id=job_id)
        over = active[: max(0, len(active) - cap)]
        for row in over:
            row.status = "evicted"
        return len(over)

    def replace_blocks(self, scope, blocks, cap, *, job_id="", task_id="", claim_token=""):
        for row in self.list_active(scope, job_id=job_id):
            row.status = "evicted"
        for block in blocks:
            self.append_and_evict(scope, block.ts, block.source, block.content, cap, job_id=job_id)

    def list_active(self, scope: OwnerScope, *, job_id: str = "") -> list[BlockRow]:
        active = [r for key, r in self.rows if key == self._key(scope, job_id) and r.status == "active"]
        return sorted(active, key=lambda r: r.id)

    def delete_partition(self, scope: OwnerScope, *, job_id: str, limit: int) -> int:
        matched = [(key, row) for key, row in self.rows if key == self._key(scope, job_id)][:limit]
        self.rows = [item for item in self.rows if item not in matched]
        return len(matched)


@pytest.fixture
def scratchpad() -> Scratchpad:
    return Scratchpad(FakeScratchpad())


def test_append_returns_blocks_in_order_and_eviction_retains_content(scratchpad):
    for i in range(SCRATCHPAD_MAX_BLOCKS + 3):
        block = scratchpad.append_block(SCOPE, f"block {i}", source="test")
        assert block["content"] == f"block {i}" and block["source"] == "test" and block["ts"]
    rows = scratchpad.list_blocks(SCOPE)
    assert len(rows) == SCRATCHPAD_MAX_BLOCKS and rows[0].content == "block 3"
    assert [row.id for row in rows] == sorted(row.id for row in rows)
    evicted = [row for _, row in scratchpad._repo.rows if row.status == "evicted"]
    assert [row.content for row in evicted] == ["block 0", "block 1", "block 2"]
    assert "block 0" not in scratchpad.render(SCOPE)


def test_block_order_is_the_id_not_the_timestamp(scratchpad):
    """Regression: block order is the id, never the displayed timestamp (D7)."""
    # Written first, but stamped far in the future: id and ts disagree.
    scratchpad._repo.append_and_evict(SCOPE, "2030-01-01T00:00:00+00:00", "task", "written first", 100)
    scratchpad.append_block(SCOPE, "written second")

    rows = scratchpad.list_blocks(SCOPE)
    assert [r.content for r in rows] == ["written first", "written second"]
    assert rows[0].ts > rows[1].ts
    rendered = scratchpad.render(SCOPE)
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
        {"ts": "2026-07-01T10:00:00+00:00", "content": "source placeholder"},
    ]
    rows = [
        BlockRow(id=i, ts=b["ts"], source=b.get("source", ""), content=b["content"])
        for i, b in enumerate(blocks, start=1)
    ]

    assert render_blocks(rows) == _legacy_render(blocks)


def test_empty_scratchpad_renders_the_default_text_without_writing(scratchpad):
    rendered = scratchpad.render(SCOPE)

    assert rendered.startswith("# Scratchpad\n\nUpdatedAt: ")
    assert rendered.endswith("\n\n(empty — write anything here)\n")
    assert scratchpad._repo.rows == []
