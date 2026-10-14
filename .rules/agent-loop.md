---
paths:
  - "ouroboros/agent/**"
  - "ouroboros/storage/task_episodes.py"
---

# Agent: task execution, context, adapters — read before editing

The concrete side of the task path. `new_core/` owns the runtime-independent graph and loop; `ouroboros/agent/` owns the adapters (`docs/ARCHITECTURE.md` §Agent loop and tools). Changing loop or graph behavior means reading `.rules/new-core.md` as well.

| Concern | Owner |
|---|---|
| Agent entrypoint and `handle_task` scaffolding | `ouroboros/agent/core.py` |
| Per-task graph context and state | `ouroboros/agent/task_graph.py` |
| Adapters for the loop interfaces | `ouroboros/agent/loop_adapters.py` |
| Model call with retry/usage, circuit reset, final-answer repair | `ouroboros/agent/loop_llm_call.py` |
| Tool-call handling, result processing, failure prefixes | `ouroboros/agent/loop_tool_execution.py` |
| Prompt section declarations: order, budgets, drop order, protection | `ouroboros/agent/context.py` (`prompt_sections`) |
| Prompt context and compaction | `ouroboros/agent/context.py`, `ouroboros/agent/context_compaction.py` |
| Durable-memory recall | `ouroboros/agent/memory_recall.py` — owner list in `.rules/core-memory.md` |
| Working retrieval and its index | `ouroboros/agent/working_retrieval.py`, `ouroboros/agent/retrieval_index.py` |
| Mid-task owner messages | `ouroboros/agent/owner_inject.py` |
| Final-response script guard | `ouroboros/agent/response_language_guard.py` |
| Ledger, continuation, receipts | `ouroboros/agent/task_ledger.py`, `ouroboros/agent/task_continuation.py`, `ouroboros/agent/tool_receipts.py` |
| Task pipeline and startup checks | `ouroboros/agent/task_pipeline.py`, `ouroboros/agent/startup_checks.py` |

**Rules:** `handle_task` keeps only scaffolding that cannot be a node — trace scope, trace span, heartbeat, temporary workspace, `finally` cleanup — around a single `invoke`. Adapters implement the loop ABCs and do not leak loop internals back into ouroboros. Invariant 1 governs task context.

One run carries one immutable-snapshot `SkillSurface`. In multi-user mode task start resolves `OUROBOROS_USER_ENABLED_SKILL_IDS` once for the complete `OwnerScope`; malformed text or read failure becomes an empty selection; absence resolves to instance `AppSettings` (default `[]`). A settings change affects the next run, never the active one. Task start does not refresh either catalog.
