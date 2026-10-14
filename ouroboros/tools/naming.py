"""Provider-safe tool names, shared by every source that names a tool after a remote thing.

Providers validate names against ``^[a-zA-Z0-9_-]{1,64}$`` (see
``sanitize_chat_completion_tools`` in ``ouroboros/llm/providers/base.py``); anything outside
that is silently dropped before it reaches the LLM. MCP composes ``mcp__{server}__{tool}``
and outbound A2A composes ``a2a__{agent}``, so the rule lives here rather than in either.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "NAME_HASH_LEN",
    "PROVIDER_NAME_MAX_LEN",
    "PROVIDER_NAME_RE",
    "cap_tool_name",
    "sanitize_tool_name",
    "short_hash",
]

PROVIDER_NAME_MAX_LEN = 64
NAME_HASH_LEN = 6

#: What a provider accepts. The producers below and the validator in
#: ``ouroboros.llm.providers.base`` read this one pattern, so a name this module composes
#: cannot become one that module silently drops.
PROVIDER_NAME_RE = re.compile(rf"^[a-zA-Z0-9_-]{{1,{PROVIDER_NAME_MAX_LEN}}}$")

_PROVIDER_UNSAFE_CHAR_RE = re.compile(r"[^a-zA-Z0-9_-]")


def short_hash(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:NAME_HASH_LEN]


def sanitize_tool_name(raw: str) -> str:
    sanitized = _PROVIDER_UNSAFE_CHAR_RE.sub("_", raw)
    if sanitized == raw:
        return sanitized
    # Replacement is lossy ("a/b" and "a.b" both become "a_b"), so a hash keeps them distinct.
    return f"{sanitized}_{short_hash(raw)}"


def cap_tool_name(full: str) -> str:
    """Fit an already-sanitised composed name inside the provider length limit."""
    if len(full) <= PROVIDER_NAME_MAX_LEN:
        return full
    keep = PROVIDER_NAME_MAX_LEN - NAME_HASH_LEN - 1
    return f"{full[:keep]}_{short_hash(full)}"
