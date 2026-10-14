"""What an owner's decision on a tool call can be, shared by the run, the reply path and the store."""

from __future__ import annotations

CONFIRMATION_PENDING = "pending"
CONFIRMATION_APPROVED = "approved"
CONFIRMATION_DENIED = "denied"
CONFIRMATION_EXPIRED = "expired"

#: A round that reached the tools with no decision for a gated call. Never a stored row.
CONFIRMATION_NOT_ASKED = "not_asked"

#: What an owner's reply may record; expiry is written by the run that withdraws its question.
CONFIRMATION_DECISIONS = frozenset({CONFIRMATION_APPROVED, CONFIRMATION_DENIED})

#: Where the request's fields ride on an outbound message's free-form transport mapping.
CONFIRMATION_TRANSPORT_KEY = "tool_confirmation"

#: The audit trail of one request: opened, then exactly one outcome. Written by the run, the reply
#: path and the sweep alike, so the names belong to none of them.
CONFIRMATION_REQUESTED_EVENT = "policy.tool_confirmation_requested"
CONFIRMATION_RESOLVED_EVENT = "policy.tool_confirmation_resolved"
CONFIRMATION_EXPIRED_EVENT = "policy.tool_confirmation_expired"

__all__ = [
    "CONFIRMATION_APPROVED",
    "CONFIRMATION_DECISIONS",
    "CONFIRMATION_DENIED",
    "CONFIRMATION_EXPIRED",
    "CONFIRMATION_EXPIRED_EVENT",
    "CONFIRMATION_NOT_ASKED",
    "CONFIRMATION_PENDING",
    "CONFIRMATION_REQUESTED_EVENT",
    "CONFIRMATION_RESOLVED_EVENT",
    "CONFIRMATION_TRANSPORT_KEY",
]
