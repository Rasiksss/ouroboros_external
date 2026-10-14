---
paths:
  - "ouroboros/storage/conversations/**"
  - "ouroboros/storage/chat_store.py"
---

# Conversations persistence — read before editing

| Concern | Owner |
|---|---|
| Message, history, summary and handoff interface | `ouroboros/storage/conversations/interfaces.py` |
| PostgreSQL adapter | `ouroboros/storage/conversations/repository.py` |
| Chat facade | `ouroboros/storage/chat_store.py` |
| Composition and lifetime | `ouroboros/storage/runtime_persistence.py` |
| SQL/InMemory contracts and transactions | `tests/ouroboros/storage/conversations/` |

The operational data flow is in `docs/ARCHITECTURE.md` §Owner chat messages.

- The adapter receives a composed transaction manager; it neither creates engines nor imports consumers.
- Keep transport and background-memory SQL in their own repositories; shared tables do not transfer ownership.
