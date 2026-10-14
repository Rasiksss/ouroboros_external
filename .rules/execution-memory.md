---
paths:
  - "ouroboros/storage/execution_memory/**"
---

# Execution memory persistence — read before editing

| Concern | Owner |
|---|---|
| Episode and invocation interface | `ouroboros/storage/execution_memory/interfaces.py` |
| PostgreSQL adapter | `ouroboros/storage/execution_memory/repository.py` |
| Composition and lifetime | `ouroboros/storage/runtime_persistence.py` |
| SQL/InMemory contracts and transaction tests | `tests/ouroboros/storage/execution_memory/` |

The operational data flow is in `docs/ARCHITECTURE.md` §Working retrieval.

- Inject the transaction manager; the adapter creates no engine and imports no consumers.
- Task admission owns snapshot commit; its connection reaches the repository through the composed manager.
- Keep supervisor retention SQL within supervisor; sharing tables does not transfer that responsibility.
