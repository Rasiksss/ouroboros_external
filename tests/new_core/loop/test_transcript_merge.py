"""The transcript-shape rules both layers share, tested directly.

The loop's nodes and the ouroboros owners inject text into the same transcript through
this one module; the rules used to exist twice and had to be kept in step by hand. These
tests pin the behavior at the module itself, not only through a caller.
"""

from __future__ import annotations

from new_core.loop.transcript_merge import (
    MERGE_SEPARATOR,
    append_or_merge_user_content,
    append_or_merge_user_message,
    merge_system_note,
)


def test_appends_when_no_trailing_user_turn() -> None:
    messages = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "hi"}]
    append_or_merge_user_message(messages, "next")
    assert messages[-1] == {"role": "user", "content": "next"}
    assert len(messages) == 3


def test_appends_to_empty_transcript() -> None:
    messages: list[dict] = []
    append_or_merge_user_message(messages, "first")
    assert messages == [{"role": "user", "content": "first"}]


def test_merges_into_trailing_string_user_turn() -> None:
    messages = [{"role": "user", "content": "first  "}]
    append_or_merge_user_message(messages, "second")
    assert messages == [{"role": "user", "content": f"first{MERGE_SEPARATOR}second"}]
    assert len(messages) == 1


def test_merge_into_empty_string_turn_keeps_no_separator() -> None:
    messages = [{"role": "user", "content": ""}]
    append_or_merge_user_message(messages, "text")
    assert messages == [{"role": "user", "content": "text"}]


def test_merges_text_into_trailing_multipart_turn_without_flattening() -> None:
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]
    append_or_merge_user_message(messages, "caption")
    assert messages[-1]["content"] == [
        {"type": "image_url", "image_url": {"url": "u"}},
        {"type": "text", "text": MERGE_SEPARATOR + "caption"},
    ]


def test_merges_multipart_content_into_multipart_turn() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    append_or_merge_user_content(messages, [{"type": "text", "text": "b"}])
    assert messages[-1]["content"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


def test_merges_multipart_content_into_string_turn_via_prefix_block() -> None:
    messages = [{"role": "user", "content": "prior  "}]
    append_or_merge_user_content(messages, [{"type": "text", "text": "b"}])
    assert messages[-1]["content"] == [
        {"type": "text", "text": "prior" + MERGE_SEPARATOR},
        {"type": "text", "text": "b"},
    ]


def test_multipart_content_appended_whole_when_no_trailing_user_turn() -> None:
    blocks = [{"type": "text", "text": "b"}]
    messages = [{"role": "assistant", "content": "hi"}]
    append_or_merge_user_content(messages, blocks)
    assert messages[-1] == {"role": "user", "content": blocks}


def test_never_produces_consecutive_user_turns() -> None:
    messages: list[dict] = []
    for text in ("one", "two", "three"):
        append_or_merge_user_message(messages, text)
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == MERGE_SEPARATOR.join(("one", "two", "three"))


def test_system_note_merges_into_leading_string_system() -> None:
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    merge_system_note(messages, "note")
    assert messages[0] == {"role": "system", "content": "sys\n\nnote"}
    assert len(messages) == 2


def test_system_note_appends_block_to_multipart_system() -> None:
    messages = [{"role": "system", "content": [{"type": "text", "text": "sys"}]}]
    merge_system_note(messages, "note")
    assert messages[0]["content"] == [{"type": "text", "text": "sys"}, {"type": "text", "text": "note"}]


def test_system_note_is_inserted_when_absent() -> None:
    messages = [{"role": "user", "content": "u"}]
    merge_system_note(messages, "note")
    assert messages[0] == {"role": "system", "content": "note"}
    assert messages[1] == {"role": "user", "content": "u"}


def test_system_note_replaces_empty_system_content() -> None:
    messages = [{"role": "system", "content": None}]
    merge_system_note(messages, "note")
    assert messages[0] == {"role": "system", "content": "note"}
