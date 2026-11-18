"""Prompt assembly substrate: declarations are data, a walk executes them.

The same seam ``new_core/graph`` draws for the task path. There, topology is data
supplied by ouroboros and the engine only runs it; here, the prompt's sections are
data supplied by ouroboros and this package only renders, shrinks, and flattens
them. Neither knows what the caller's nodes or sections are about.

This package therefore names no section and imports nothing outside the stdlib
(invariant 4, pinned by ``tests/new_core/prompt/test_import_boundary.py``). Which
sections exist, what they contain, what their budgets are, and which block holds
them all live in ``ouroboros/agent/context.py``, because they are decisions about
this product's prompt rather than about how a prompt is assembled.
"""

from __future__ import annotations

from new_core.prompt.assemble import (
    apply_fallbacks,
    assemble_blocks,
    render_sections,
    shrink_sections,
    truncate_tail,
)
from new_core.prompt.types import (
    PromptBlock,
    PromptSection,
    RenderedSection,
    SectionBuilderFn,
    SectionTruncateFn,
)

__all__ = [
    "PromptBlock",
    "PromptSection",
    "RenderedSection",
    "SectionBuilderFn",
    "SectionTruncateFn",
    "apply_fallbacks",
    "assemble_blocks",
    "render_sections",
    "shrink_sections",
    "truncate_tail",
]
