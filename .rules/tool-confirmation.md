---
paths:
  - "ouroboros/storage/tool_confirmation/**"
---

# Tool confirmation persistence — read before editing

Behavior lives in `openspec/specs/tool-confirmation/spec.md`; the runtime flow is mapped in
`docs/ARCHITECTURE.md` §Owner confirmation of a tool call.

| Concern | Owner |
|---|---|
| Repository contract and snapshot/argument shapes | `ouroboros/storage/tool_confirmation/interfaces.py` |
| Confirmation reads/writes and parked-run persistence | `ouroboros/storage/tool_confirmation/repository.py` |
| Shared SQL transaction management | `infra/sql_transaction.py` |
| SQL and InMemory conformance, rollback and concurrency checks | `tests/ouroboros/storage/tool_confirmation/` |
| Consumer test double | `tests/helpers/tool_confirmations.py` |

**Rules:**

- Keep persistence behind `IToolConfirmationRepository` and inject `SqlTransactionManager` into its SQL implementation;
  rebuilding a runtime store inside the repository bypasses the caller's transaction.
- Preserve transaction boundaries when editing confirmation, task and audit writes together;
  `tests/ouroboros/storage/tool_confirmation/test_transactions.py` checks atomicity and rollback.

Retire this rule when the package no longer owns confirmation and parked-run persistence; move its routing to the new owner.
