"""Shared chat id policy for human-visible and synthetic conversations.

A negative chat id is a routing fact only: it selects bridge subscriptions and
suppresses live delivery to browsers and dialogs. It hides nothing from history,
which is decided by ``messages.chat_history_visible`` — "not a progress message".

Two allocators share the negative range and must not meet: the skill bridge hands
out ``[SKILL_BRIDGE_CHAT_ID_MIN, SKILL_BRIDGE_CHAT_ID_MAX]``, and
``a2a_executor.derive_a2a_chat_id`` derives at ``A2A_CHAT_ID_CEILING`` and below.
``is_internal_chat_id`` ignores the split — legacy A2A minted unbounded negatives,
and every one of them is still internal.
"""

from __future__ import annotations

WEB_UI_CHAT_ID = 1

#: The skill bridge's allocation window.
SKILL_BRIDGE_CHAT_ID_MIN = -3999
SKILL_BRIDGE_CHAT_ID_MAX = -1

#: The highest id A2A derivation may produce — one below the skill bridge's window, so the
#: two allocators cannot collide. Derived rather than restated: widening the window above
#: must move this with it.
A2A_CHAT_ID_CEILING = SKILL_BRIDGE_CHAT_ID_MIN - 1


def _coerce_chat_id_int(chat_id: object) -> int | None:
    if isinstance(chat_id, bool):
        return int(chat_id)
    if isinstance(chat_id, int):
        return chat_id
    if isinstance(chat_id, (str, bytes, bytearray)):
        try:
            return int(chat_id)
        except ValueError:
            return None
    return None


def is_internal_chat_id(chat_id: object) -> bool:
    """Return True for synthetic chat ids: agent-to-agent traffic and skill bridges alike."""
    value = _coerce_chat_id_int(chat_id)
    if value is None:
        return False
    return value < 0


def is_derived_a2a_chat_id(chat_id: object) -> bool:
    """Return True only for ids this build's A2A derivation could have produced.

    Narrower than :func:`is_internal_chat_id` on purpose: this one decides how a row is
    *grouped*, and grouping a skill bridge's conversation as an A2A task would be a claim
    about where it came from.
    """
    value = _coerce_chat_id_int(chat_id)
    if value is None:
        return False
    return value <= A2A_CHAT_ID_CEILING


__all__ = [
    "A2A_CHAT_ID_CEILING",
    "SKILL_BRIDGE_CHAT_ID_MAX",
    "SKILL_BRIDGE_CHAT_ID_MIN",
    "WEB_UI_CHAT_ID",
    "is_derived_a2a_chat_id",
    "is_internal_chat_id",
]
