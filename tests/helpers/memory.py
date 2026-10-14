"""Memory domain test constants extracted from test_memory_domain."""

from __future__ import annotations

from ouroboros.storage.core_memory.memory.domain.models import kinds_with_injection
from ouroboros.storage.core_memory.memory.domain.render import IndexSection

_RECALL_KINDS = kinds_with_injection("recall_auto", "recall_selector")
_INDEX_ONLY_KINDS = kinds_with_injection("index_only")

MEMORY_SECTION = IndexSection("## Memory", _RECALL_KINDS, 150)
KNOWLEDGE_SECTION = IndexSection("## Knowledge", _INDEX_ONLY_KINDS, 100)
