"""Scratchpad domain service — the public working-memory API.

All business rules live here: the block cap, timestamp minting, the newest-first
render, and the empty-state default. Persistence is delegated to an
``IScratchpad`` port so the same rules run over PostgreSQL or a test fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.render import render_blocks
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow, IScratchpad
from ouroboros.utils import utc_now_iso


class Scratchpad:
    """Block-structured working memory.

    Stateless with respect to identity: every method takes the ``OwnerScope`` of the
    run asking, and hands it to the port. Two agents of one owner keep separate
    buffers, so neither's appends evict the other's blocks.
    """

    def __init__(self, repo: IScratchpad):
        self._repo = repo

    # ------------------------------------------------------------------ blocks

    def append_block(
        self,
        scope: OwnerScope,
        content: str,
        source: str = "task",
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> dict[str, Any]:
        """Append one block and evict down to the cap. Returns the written block.

        The timestamp is minted here, so the caller gets it back without the
        port needing to report the row it just inserted. The cap is passed to
        the port rather than enforced by a second call: the append and the
        eviction it triggers are one transaction, so the buffer is never
        observable over its cap and a failure cannot leave it there.
        """
        ts = utc_now_iso()
        self._repo.append_and_evict(
            scope, ts, source, content, SCRATCHPAD_MAX_BLOCKS, job_id=job_id, task_id=task_id, claim_token=claim_token
        )
        return {"ts": ts, "source": source, "content": content}

    def replace_blocks(
        self,
        scope: OwnerScope,
        blocks: Sequence[BlockRow],
        *,
        job_id: str = "",
        task_id: str = "",
        claim_token: str = "",
    ) -> None:
        self._repo.replace_blocks(
            scope, blocks, SCRATCHPAD_MAX_BLOCKS, job_id=job_id, task_id=task_id, claim_token=claim_token
        )

    def list_blocks(self, scope: OwnerScope, *, job_id: str = "") -> list[BlockRow]:
        """Return the active blocks, oldest-first."""
        return self._repo.list_active(scope, job_id=job_id)

    def delete_partition(self, scope: OwnerScope, *, job_id: str = "", limit: int) -> int:
        if not job_id:
            raise ValueError("the foreground scratchpad cannot be deleted as a job partition")
        return self._repo.delete_partition(scope, job_id=job_id, limit=limit)

    # ------------------------------------------------------------------ render

    def render(self, scope: OwnerScope, *, job_id: str = "") -> str:
        """Return the scratchpad view, or the default text when it is empty.

        Unlike the file-backed version this replaces, rendering never writes.
        """
        return render_blocks(self.list_blocks(scope, job_id=job_id))


__all__ = ["Scratchpad"]
