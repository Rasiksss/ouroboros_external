---
paths:
  - "ouroboros/storage/core_memory/**"
  - "ouroboros/tools/memory_tools.py"
  - "ouroboros/tools/service/memory.py"
  - "ouroboros/agent/memory_recall.py"
---

# Core memory — read before editing

Capabilities over a shared runtime DB, sharing a SQL adapter base.

## Durable named memory (facts and knowledge)

One runtime-DB table keyed by `kind`. **The index is a query, never a file.**

| Concern | Owner |
|---|---|
| Storage | `ouroboros/storage/core_memory/memory/` |
| Migration | `infra/migrations/` (core memory) |
| Tools | `ouroboros/tools/memory_tools.py` |
| Recall | `ouroboros/agent/memory_recall.py` |
| Prompt section builders | `ouroboros/agent/context.py` |
| `_migrated_memory_owner` refusal | `ouroboros/tools/core.py` |

## Scratchpad working memory

Blocks in the runtime DB, not blob storage: `ouroboros/storage/core_memory/scratchpad/`, the shared
migration, and `ouroboros/tools/service/memory.py`.

## Shared plumbing

`ouroboros/storage/core_memory/_shared/` is the SQL adapter base — **changing it touches every
capability.** The engine behind it is `ouroboros/storage/runtime_engine.py`, shared with user
settings, so a change there also affects `ouroboros/storage/settings/`.
