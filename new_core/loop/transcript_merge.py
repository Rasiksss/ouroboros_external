"""Merging turns into an OpenAI-shape transcript.

Shared by everything that injects text into a running transcript: the loop's own nodes
(the skill-finalization reminder, the round-limit notice) and the ouroboros owners —
the event-sink adapter (self-check turns), the owner-inbox adapter (owner messages),
and the task-graph builder (the system note about non-core tools). The rules live in
one place because they must not drift between those paths: no consecutive same-role
turns (Anthropic returns 400s for them), and multipart content blocks are preserved,
never flattened.

The module sits on the ``new_core`` side of the seam rather than in ``ouroboros/agent``
because ``new_core/loop`` may not import ``ouroboros`` (see the loop's import-boundary
test), and a lower-layer home is the only one both sides can reach — the same reasoning
that moved the LLM wire types into ``new_core/llm``.
"""

from __future__ import annotations

from typing import Any

#: What separates two user turns folded into one. Public because merging is only half the
#: story: an owner that later withdraws its own text has to recognize the boundary it was
#: merged across, and guessing at it would leave the separator behind as debris.
MERGE_SEPARATOR = "\n\n---\n\n"


def append_or_merge_user_message(messages: list[dict[str, Any]], text: str) -> None:
    """Append a user message without creating consecutive user turns."""
    append_or_merge_user_content(messages, text)


def append_or_merge_user_content(messages: list[dict[str, Any]], content: Any) -> None:
    """Append user content without flattening multipart blocks."""
    if messages and messages[-1].get("role") == "user":
        prior = messages[-1].get("content")
        if isinstance(content, list):
            new_blocks = list(content)
            if isinstance(prior, list):
                messages[-1] = {"role": "user", "content": list(prior) + new_blocks}
                return
            prior_text = prior if isinstance(prior, str) else str(prior or "")
            prefix_block = [{"type": "text", "text": prior_text.rstrip() + MERGE_SEPARATOR}] if prior_text else []
            messages[-1] = {"role": "user", "content": prefix_block + new_blocks}
            return
        text = str(content or "")
        if isinstance(prior, list):
            messages[-1] = {
                "role": "user",
                "content": [*list(prior), {"type": "text", "text": MERGE_SEPARATOR + text}],
            }
            return
        prior_text = prior if isinstance(prior, str) else str(prior or "")
        messages[-1] = {
            "role": "user",
            "content": (prior_text.rstrip() + MERGE_SEPARATOR + text) if prior_text else text,
        }
        return
    messages.append({"role": "user", "content": content})


def merge_system_note(messages: list[dict[str, Any]], note: str) -> None:
    """Merge a note into the leading system message, creating it if absent."""
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": note})
        return

    content = messages[0].get("content")
    if isinstance(content, list):
        content.append({"type": "text", "text": note})
    elif isinstance(content, str):
        messages[0]["content"] = f"{content}\n\n{note}" if content else note
    elif content is None:
        messages[0]["content"] = note
    else:
        messages[0]["content"] = f"{content}\n\n{note}" if content else note


__all__ = [
    "MERGE_SEPARATOR",
    "append_or_merge_user_content",
    "append_or_merge_user_message",
    "merge_system_note",
]
