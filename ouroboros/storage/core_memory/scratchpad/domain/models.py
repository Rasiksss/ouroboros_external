"""Domain constants for scratchpad working memory."""

from __future__ import annotations

#: FIFO cap on active blocks per owner; overflow is evicted, never deleted.
SCRATCHPAD_MAX_BLOCKS = 10

#: Shown when no active block exists, in place of the rendered view.
DEFAULT_SCRATCHPAD_TEMPLATE = "# Scratchpad\n\nUpdatedAt: {updated_at}\n\n(empty — write anything here)\n"

__all__ = ["DEFAULT_SCRATCHPAD_TEMPLATE", "SCRATCHPAD_MAX_BLOCKS"]
