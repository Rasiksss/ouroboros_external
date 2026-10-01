"""Rendering of the scratchpad view.

The output format is load-bearing: it is what the agent sees in context and
through ``data_read("memory/scratchpad.md")``. Both functions are ports of
``Memory.regenerate_scratchpad_md`` / ``Memory._default_scratchpad`` and must
stay byte-identical to them.
"""

from __future__ import annotations

from collections.abc import Sequence

from ouroboros.storage.core_memory.scratchpad.domain.models import (
    DEFAULT_SCRATCHPAD_TEMPLATE,
    SCRATCHPAD_MAX_BLOCKS,
)
from ouroboros.storage.core_memory.scratchpad.interfaces.scratchpad import BlockRow
from ouroboros.utils import utc_now_iso


def default_scratchpad() -> str:
    """Return the empty-scratchpad text."""
    return DEFAULT_SCRATCHPAD_TEMPLATE.format(updated_at=utc_now_iso())


def render_blocks(blocks: Sequence[BlockRow]) -> str:
    """Render active blocks newest-first, or the default text when there are none."""
    if not blocks:
        return default_scratchpad()

    n = len(blocks)
    parts = [f"## Scratchpad (working memory — {n}/{SCRATCHPAD_MAX_BLOCKS} blocks)\n"]
    for block in reversed(blocks):
        ts = str(block.ts or "")[:16]
        source = block.source or "?"
        parts.append(f"### [{ts} — {source}]\n{block.content or ''}\n\n---\n")

    return "\n".join(parts)


__all__ = ["default_scratchpad", "render_blocks"]
