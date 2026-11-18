"""Core memory capabilities.

Each capability is a self-contained package with the same three layers
(``interfaces/`` I/O port, ``domain/`` rules, ``infra/`` adapters) and a
module-level factory. Both back onto the runtime DB:

- ``memory`` — every durable named memory (``get_memory()``):
  long-term facts, and operational knowledge, distinguished by ``kind`` rather
  than by subsystem
- ``scratchpad`` — block-structured working memory (``get_scratchpad()``)

The split between them is not storage but lifetime and shape: a memory is a
named document the agent addresses on purpose, while the scratchpad is an
unnamed, database-ordered block buffer with FIFO eviction.

``_shared/`` holds the plumbing both need — runtime-engine resolution and the
SQL adapter base — and nothing capability-specific.
"""
