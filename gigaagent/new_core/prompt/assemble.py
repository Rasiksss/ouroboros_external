"""Render declared sections, shrink them to fit, then flatten them into blocks.

Three steps, in this order, and the order is the point: shrinking happens while
sections are still separate values. Nothing here ever recovers a section boundary
by parsing assembled text, so free-form content cannot start, end, or absorb a
section however much it looks like a heading.

Pure and stdlib-only: no I/O, no logging, no clock. Over-budget reports and the
trimming record are returned to the caller, which owns what to do with them.
"""

from __future__ import annotations

from typing import Any

from new_core.prompt.types import PromptBlock, PromptSection, RenderedSection, TInputs


def render_sections(
    sections: list[PromptSection[TInputs]],
    inputs: TInputs,
) -> tuple[list[RenderedSection], list[tuple[str, int, int]]]:
    """Build every section in declaration order.

    Returns the sections that produced text, and one ``(name, size, budget)``
    report per section that exceeded its declared budget. Reporting rather than
    shrinking is deliberate: a build-time budget here has always been a warning,
    and turning it into a limit is a change to what the model receives, not a
    change to how sections are declared.
    """
    rendered: list[RenderedSection] = []
    over_budget: list[tuple[str, int, int]] = []
    for section in sections:
        text = section.build(inputs)
        if section.budget is not None and len(text) > section.budget:
            over_budget.append((section.name, len(text), section.budget))
        if not text.strip():
            continue
        rendered.append(RenderedSection(section=section, text=text))
    return rendered, over_budget


def truncate_tail(text: str, max_chars: int, unit_delimiter: str = "\n") -> str:
    """Keep the section's heading and as much of its tail as fits.

    Whole units are dropped from the front until the body fits, so a section is
    never cut mid-unit. The default when a section declares no truncation of its
    own; a section whose content has internal structure declares one instead.

    A final unit that is itself over the limit is cut mid-unit rather than kept:
    the caller's soft cap stops once a section reports as truncated, so returning
    an untrimmed body here would end the whole pass still over budget.
    """
    header, _, body = text.partition("\n")
    if len(text) <= max_chars:
        return text
    body = body.strip("\n")
    if len(body) <= max_chars:
        return text
    units = body.split(unit_delimiter)
    while len(units) > 1 and len(unit_delimiter.join(units)) > max_chars:
        units.pop(0)
    kept = unit_delimiter.join(units).lstrip()
    if len(kept) > max_chars:
        kept = kept[len(kept) - max_chars :].lstrip()
    return f"{header}\n\n[... {len(body) - len(kept)} chars omitted]\n{kept}"


def _assembled_length(rendered: list[RenderedSection], separator_chars: int) -> int:
    """What the survivors will occupy once joined — separators included."""
    if not rendered:
        return 0
    return sum(len(item.text) for item in rendered) + separator_chars * (len(rendered) - 1)


def shrink_sections(
    rendered: list[RenderedSection],
    *,
    target_chars: int,
    separator_chars: int = 0,
) -> tuple[list[RenderedSection], list[str], list[str]]:
    """Drop or truncate droppable sections until the total fits ``target_chars``.

    Returns ``(survivors, dropped_names, truncated_names)``. Droppable sections
    are considered in ascending ``drop_priority``; a section declaring a
    ``min_chars`` is truncated to it and kept, one declaring none is removed
    whole. Sections declaring no priority are never touched — protection is the
    absence of a priority, so there is no protected list to disagree with.

    ``separator_chars`` is what the caller will put between sections, so the
    measure is the length of the text this actually produces rather than of the
    sections alone. Splitting text apart to trim it could only ever approximate
    that, because the separators had already been absorbed into the pieces.
    """
    survivors = list(rendered)
    dropped: list[str] = []
    truncated: list[str] = []
    droppable = sorted(
        (item for item in survivors if item.section.drop_priority is not None),
        key=lambda item: item.section.drop_priority or 0,
    )
    for candidate in droppable:
        if _assembled_length(survivors, separator_chars) <= target_chars:
            break
        index = survivors.index(candidate)
        section = candidate.section
        if section.min_chars > 0 and len(candidate.text) > section.min_chars:
            text = (
                section.truncate(candidate.text, section.min_chars)
                if section.truncate
                else truncate_tail(candidate.text, section.min_chars, section.unit_delimiter)
            )
            survivors[index] = RenderedSection(section=section, text=text)
            truncated.append(section.name)
        else:
            dropped.append(section.name)
            survivors.pop(index)
    return survivors, dropped, truncated


def apply_fallbacks(rendered: list[RenderedSection]) -> tuple[list[RenderedSection], list[str]]:
    """Replace every section declaring a fallback with it; report which changed.

    The last resort, for sections a caller cannot drop — one in a cached block,
    say, whose position is load-bearing but whose bulk is not.
    """
    result = list(rendered)
    replaced: list[str] = []
    for index, item in enumerate(result):
        fallback = item.section.fallback
        if fallback is None or item.text == fallback:
            continue
        result[index] = RenderedSection(section=item.section, text=fallback)
        replaced.append(item.section.name)
    return result, replaced


def assemble_blocks(
    blocks: list[PromptBlock],
    rendered: list[RenderedSection],
    *,
    separator: str = "\n\n",
) -> list[dict[str, Any]]:
    """Flatten surviving sections into content blocks, in declared block order.

    Called last, after shrinking, so section boundaries are still known while the
    decisions that need them are being made. A block whose sections all produced
    nothing is omitted rather than emitted empty.
    """
    content: list[dict[str, Any]] = []
    for block in blocks:
        texts = [item.text for item in rendered if item.section.block == block.key]
        if not texts:
            continue
        entry: dict[str, Any] = {"type": "text", "text": separator.join(texts)}
        if block.cache_control is not None:
            entry["cache_control"] = dict(block.cache_control)
        content.append(entry)
    return content


__all__ = [
    "apply_fallbacks",
    "assemble_blocks",
    "render_sections",
    "shrink_sections",
    "truncate_tail",
]
