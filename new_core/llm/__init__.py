"""The LLM wire types the loop and its adapters both name.

Moved out of ``ouroboros/llm`` so that ``new_core/loop`` can type its LLM-facing
interface — ``LLMProvider.call`` returns an ``LLMMessage``, ``RunState.last_resp``
holds one — without importing ``ouroboros`` (see the loop's import-boundary test).
Like ``graph`` and ``loop``, and unlike ``tools``, this package depends only on the
Python standard library.
"""

from __future__ import annotations

from new_core.llm.types import LLMMessage, ToolCall, ToolCallFunction
from new_core.llm.usage import add_usage

__all__ = ["LLMMessage", "ToolCall", "ToolCallFunction", "add_usage"]
