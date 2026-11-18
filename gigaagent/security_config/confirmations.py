"""Tools that need the owner's decision before they run — the security-owned source of the list.

Mirrors `mcp.py` and `a2a.py`: a change goes through code review, CI and image signing. Whether the
gate is active at all is the owner's setting; what it covers is not.
"""

#: Tools whose effect outlives the run or reaches past the run's own conversation.
confirmation_required_tools: frozenset[str] = frozenset(
    {
        "code_exec",
        "file_write",
        "file_edit",
        "apply_patch",
        "data_write",
    }
)

#: Descriptor origins whose tools are gated whatever their generated names are.
confirmation_required_origins: tuple[str, ...] = ("a2a:",)

#: Origins the prefixes above would otherwise catch: `a2a:*` only reads and withdraws a delegation.
confirmation_exempt_origins: frozenset[str] = frozenset({"a2a:*"})
