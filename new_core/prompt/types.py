"""Declaration types for prompt assembly.

The substrate is generic over one caller-defined type:

- ``TInputs`` — whatever a builder needs to produce its text (an env, a task, a
  scope, ...). The walk treats it as fully opaque and only hands it to builders,
  exactly as the graph engine treats ``TState`` and ``TContext``.

Section *behavior* is a callable, not an interface: a builder is a value the
caller supplies, the way a node is supplied to the graph, not an effect this
layer reaches out to. The ABCs in ``new_core/loop`` are for the latter.

Nothing here names a section. Which sections exist, what they contain, and which
block holds them is caller data; this module only describes the shape that data
takes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

#: Builder inputs, opaque to the walk.
TInputs = TypeVar("TInputs")

#: Produces one section's text. Returning ``""`` means "this section has nothing
#: to say this time" and the section is left out entirely.
SectionBuilderFn = Callable[[TInputs], str]

#: Shrinks one section's text to fit ``max_chars``. Declared by sections whose
#: content has internal structure that must survive shrinking; everything else
#: gets :func:`new_core.prompt.assemble.truncate_tail`.
SectionTruncateFn = Callable[[str, int], str]


@dataclass(frozen=True)
class PromptBlock:
    """One content block of the assembled prompt, in prefix order.

    ``cache_control`` is passed through verbatim onto the emitted block and is
    never inspected — whether a block is cached, and under what policy, is the
    caller's decision. A block with no section that produced text is omitted.
    """

    key: str
    cache_control: dict[str, Any] | None = None


@dataclass(frozen=True)
class PromptSection(Generic[TInputs]):
    """One section's declaration: what builds it, where it goes, how it shrinks.

    One record per section is the whole point: assembly walks this list and
    trimming sorts it, so the two cannot disagree about what a section is called,
    where it belongs, or whether it may be dropped. When those lived in separate
    tables keyed by heading prefix, they could — and the prefix table doubled as a
    parser configuration for text that had already been assembled.

    ``drop_priority`` is the protection: a section that declares none is never
    dropped or truncated. There is no second set of protected names to fall out
    of step with this one.
    """

    name: str
    heading: str
    block: str
    build: SectionBuilderFn[TInputs]
    #: Chars above which the section is reported to the walk's budget hook. A
    #: report, not a limit — nothing here shrinks a section at build time.
    budget: int | None = None
    #: ``None`` means protected. Lower priorities are acted on first.
    drop_priority: int | None = None
    #: ``0`` means "drop whole"; above zero, shrink to this many chars instead.
    min_chars: int = 0
    #: The unit the default truncation drops whole, from the front.
    unit_delimiter: str = "\n"
    #: Replacement text used when the prompt is still over budget after trimming.
    fallback: str | None = None
    truncate: SectionTruncateFn | None = None


@dataclass(frozen=True)
class RenderedSection:
    """A declaration together with the text this run's builder produced for it."""

    section: PromptSection[Any]
    text: str

    @property
    def name(self) -> str:
        return self.section.name


__all__ = [
    "PromptBlock",
    "PromptSection",
    "RenderedSection",
    "SectionBuilderFn",
    "SectionTruncateFn",
    "TInputs",
]
