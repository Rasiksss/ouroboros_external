"""Scratchpad domain service — the public working-memory API.

All business rules live here: the block cap, timestamp minting, the newest-first
render, and the empty-state default. Persistence is delegated to an
``IScratchpad`` port so the same rules run over SQLite, PostgreSQL, or a test
fake.
"""

from __future__ import annotations

from typing import Any

from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.render import render_blocks
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad
from ouroboros.utils import utc_now_iso


class Scratchpad:
    """Block-structured working memory.

    The production path uses ``owner_id="global"``; the parameter remains for
    explicit reads in tests and maintenance tools.
    """

    def __init__(self, repo: IScratchpad, owner_id: str = "global"):
        self._repo = repo
        self._owner = str(owner_id or "global").strip() or "global"

    # ------------------------------------------------------------------ blocks

    def append_block(self, content: str, source: str = "task") -> dict[str, Any]:
        """Append one block and evict down to the cap. Returns the written block.

        The timestamp is minted here, so the caller gets it back without the
        port needing to report the row it just inserted. The cap is passed to
        the port rather than enforced by a second call: the append and the
        eviction it triggers are one transaction, so the buffer is never
        observable over its cap and a failure cannot leave it there.
        """
        ts = utc_now_iso()
        self._repo.append_and_evict(self._owner, ts, source, content, SCRATCHPAD_MAX_BLOCKS)
        return {"ts": ts, "source": source, "content": content}

    def list_blocks(self) -> list[BlockRow]:
        """Return the active blocks, oldest-first."""
        return self._repo.list_active(self._owner)

    # ------------------------------------------------------------------ render

    def render(self) -> str:
        """Return the scratchpad view, or the default text when it is empty.

        Unlike the file-backed version this replaces, rendering never writes.
        """
        return render_blocks(self.list_blocks())


__all__ = ["Scratchpad"]
