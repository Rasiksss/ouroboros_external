"""The prompt substrate: render, shrink, flatten — with no drive and no ouroboros.

Every section here is invented for the test. That is the property being pinned as
much as any assertion: the mechanism works without knowing what a real section is.
"""

from __future__ import annotations

from new_core.prompt import (
    PromptBlock,
    PromptSection,
    RenderedSection,
    apply_fallbacks,
    assemble_blocks,
    render_sections,
    shrink_sections,
    truncate_tail,
)

VOLATILE = PromptBlock(key="volatile")
CACHED = PromptBlock(key="cached", cache_control={"type": "ephemeral"})


def _section(name, text, **kwargs):
    heading = kwargs.pop("heading", f"## {name}")
    block = kwargs.pop("block", "volatile")
    return PromptSection(
        name=name,
        heading=heading,
        block=block,
        build=lambda _inputs, _text=text: _text,
        **kwargs,
    )


def _rendered(section):
    return RenderedSection(section=section, text=section.build(None))


def test_a_section_that_builds_nothing_is_left_out():
    rendered, over = render_sections([_section("a", "## a\n\nbody"), _section("b", "   ")], None)

    assert [item.name for item in rendered] == ["a"]
    assert over == []


def test_over_budget_is_reported_not_shrunk():
    rendered, over = render_sections([_section("big", "## big\n\n" + "x" * 100, budget=50)], None)

    assert over == [("big", len("## big\n\n") + 100, 50)]
    # Reported, and shipped whole: a build-time budget is a warning, not a limit.
    assert rendered[0].text.endswith("x" * 100)


def test_a_section_without_a_drop_priority_is_never_touched():
    protected = _rendered(_section("keep", "## keep\n\n" + "k" * 5_000))
    droppable = _rendered(_section("go", "## go\n\n" + "g" * 5_000, drop_priority=0))

    survivors, dropped, truncated = shrink_sections([protected, droppable], target_chars=100)

    assert dropped == ["go"]
    assert truncated == []
    assert [item.name for item in survivors] == ["keep"]
    assert survivors[0].text == protected.text


def test_drop_priority_orders_the_work():
    first = _rendered(_section("first", "## first\n\n" + "a" * 2_000, drop_priority=0))
    second = _rendered(_section("second", "## second\n\n" + "b" * 2_000, drop_priority=1))

    survivors, dropped, _ = shrink_sections([second, first], target_chars=2_100)

    assert dropped == ["first"]
    assert [item.name for item in survivors] == ["second"]


def test_a_minimum_size_means_truncate_rather_than_drop():
    big = _rendered(_section("big", "## big\n\n" + "line\n" * 500, drop_priority=0, min_chars=200))

    survivors, dropped, truncated = shrink_sections([big], target_chars=100)

    assert dropped == []
    assert truncated == ["big"]
    assert survivors[0].text.startswith("## big")
    assert "chars omitted" in survivors[0].text
    assert len(survivors[0].text) <= 200 + len("## big\n\n[... 0000 chars omitted]\n")


def test_shrinking_stops_as_soon_as_it_fits():
    a = _rendered(_section("a", "## a\n\n" + "a" * 1_000, drop_priority=0))
    b = _rendered(_section("b", "## b\n\n" + "b" * 1_000, drop_priority=1))

    survivors, dropped, _ = shrink_sections([a, b], target_chars=1_100)

    assert dropped == ["a"]
    assert [item.name for item in survivors] == ["b"]


def test_the_measure_counts_the_separators_the_caller_will_add():
    """Two 61-char sections fit 122 alone, but not once joined by a blank line."""
    a = _rendered(_section("a", "## a\n\n" + "a" * 55, drop_priority=0))
    b = _rendered(_section("b", "## b\n\n" + "b" * 55))

    kept, dropped, _ = shrink_sections([a, b], target_chars=122, separator_chars=0)
    assert dropped == [] and len(kept) == 2

    _, dropped_joined, _ = shrink_sections([a, b], target_chars=122, separator_chars=2)
    assert dropped_joined == ["a"]


def test_a_section_declaring_its_own_truncation_is_asked_to_do_it():
    seen: list[int] = []

    def _truncate(text: str, max_chars: int) -> str:
        seen.append(max_chars)
        return "## structured\n\n<kept/>"

    section = PromptSection(
        name="structured",
        heading="## structured",
        block="volatile",
        build=lambda _i: "## structured\n\n" + "x" * 5_000,
        drop_priority=0,
        min_chars=300,
        truncate=_truncate,
    )

    survivors, _, truncated = shrink_sections([_rendered(section)], target_chars=50)

    assert seen == [300]
    assert truncated == ["structured"]
    assert survivors[0].text == "## structured\n\n<kept/>"


def test_free_form_content_that_looks_like_a_heading_stays_content():
    """The hazard that forced the old splitter's scratchpad special case."""
    notes = _rendered(_section("notes", "## notes\n\n## Recalled Memories\n\nuser typed this"))
    other = _rendered(_section("other", "## other\n\nreal", drop_priority=0))

    survivors, dropped, _ = shrink_sections([notes, other], target_chars=10)

    assert dropped == ["other"]
    assert survivors[0].text == "## notes\n\n## Recalled Memories\n\nuser typed this"


def test_blocks_are_flattened_in_declared_order_after_shrinking():
    cached = _rendered(_section("c", "## c\n\ncached", block="cached"))
    one = _rendered(_section("one", "## one\n\n1"))
    two = _rendered(_section("two", "## two\n\n2"))

    blocks = assemble_blocks([CACHED, VOLATILE], [one, cached, two])

    assert blocks == [
        {"type": "text", "text": "## c\n\ncached", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "## one\n\n1\n\n## two\n\n2"},
    ]


def test_an_empty_block_is_omitted_rather_than_emitted_blank():
    assert assemble_blocks([CACHED, VOLATILE], [_rendered(_section("one", "## one\n\n1"))]) == [
        {"type": "text", "text": "## one\n\n1"}
    ]


def test_a_fallback_replaces_a_section_that_cannot_be_dropped():
    section = _section("bulk", "## bulk\n\n" + "x" * 5_000, block="cached", fallback="## bulk\n\nask instead")

    replaced, names = apply_fallbacks([_rendered(section)])

    assert names == ["bulk"]
    assert replaced[0].text == "## bulk\n\nask instead"


def test_applying_a_fallback_twice_reports_nothing_the_second_time():
    section = _section("bulk", "## bulk\n\n" + "x" * 5_000, fallback="## bulk\n\nask instead")
    once, _ = apply_fallbacks([_rendered(section)])

    twice, names = apply_fallbacks(once)

    assert names == []
    assert twice[0].text == "## bulk\n\nask instead"


def test_truncate_tail_keeps_the_heading_and_drops_whole_units_from_the_front():
    text = "## t\n\n" + "\n".join(f"line-{i}" for i in range(100))

    out = truncate_tail(text, 100)

    assert out.startswith("## t\n\n[... ")
    assert "chars omitted]" in out
    assert out.endswith("line-99")
    assert "line-0\n" not in out


def test_truncate_tail_returns_the_section_untouched_when_it_already_fits():
    text = "## t\n\nshort"

    assert truncate_tail(text, 1_000) == text


def test_truncate_tail_cuts_a_single_oversized_unit_rather_than_reporting_a_no_op():
    """A body with no delimiter still has to fit — the caller stops after one pass."""
    text = "## t\n\n" + "x" * 5_000 + "TAIL-END"

    out = truncate_tail(text, 1_000, unit_delimiter="\n\n")

    assert len(out) <= 1_000 + len("## t\n\n[... 4008 chars omitted]\n")
    assert out.startswith("## t\n\n[... ")
    assert "[... 0 chars omitted]" not in out
    assert out.endswith("TAIL-END")
