---
paths:
  - "ouroboros/storage/tasks/**"
  - "ouroboros/storage/create_root_task/**"
  - "ouroboros/storage/result_delivery/**"
---

# Task lifecycle, admission and result delivery — read before editing

| Concern | Owner |
|---|---|
| Task execution and stop-state interface | `ouroboros/storage/tasks/interfaces.py` |
| PostgreSQL task lifecycle adapter | `ouroboros/storage/tasks/repository.py` |
| Terminal-memory processing markers | `ouroboros/storage/tasks/repository.py` |
| Task record encoding shared with admission | `ouroboros/storage/tasks/records.py` |
| Root admission and context capture | `ouroboros/storage/create_root_task/` |
| Publication claims and delivery receipts | `ouroboros/storage/result_delivery/` |
| Composition and lifetime | `ouroboros/storage/runtime_persistence.py` |
| SQL transactions and lifecycle tests | `tests/ouroboros/storage/tasks/` |

The operational data flow is in `docs/ARCHITECTURE.md` §Task record lifecycle.

- Inject the transaction manager; the adapter exposes no engine and imports no context repositories.
- Admission/context capture belongs to `ouroboros/storage/create_root_task/`; delivery belongs to `ouroboros/storage/result_delivery/`.
- Terminal-memory processing markers temporarily belong here; episodes remain in execution memory.
- Preserve claim-guard lifetime through commit and retry recovery after an uncertain commit.
- Keep consumer-owned repositories separate; sharing task rows does not transfer their ownership.
- Owner admission, context capture and insertion share one connection; preserve owner-wide duplicate detection and quotas.
- Publication preserves current-attempt, claim-token and live-lease guards, including after row-lock waits.
- Delivery receipts are monotonic across retries; completion checks every attachment.
