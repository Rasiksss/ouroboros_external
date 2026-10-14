# Known decisions and deferred work

This is a developer-maintained index of decisions, review conclusions, and known
work intentionally left outside the current scope. It is not the active task queue.

## Use

- Before proposing or making a change in a concrete code area, search this file
  by the relevant path and terms; read only matching records.
- A deferred record is a scope boundary until its revisit condition is met or a
  developer explicitly changes the decision.
- Developers add and update records manually. An agent must not add speculative
  items or edit this file unless explicitly asked.
- Keep one decision per row. Update its status instead of duplicating it.

## Deferred

| ID | Added | Scope | Decision | Revisit when | Status |
|---|---|---|---|---|---|
| BL-001 | 2026-09-15 | `new_core/tools/providers/extension.py::is_extension_live` | The current shape is known technical debt, but is intentionally not changed now. | A task explicitly covers extension lifecycle/design. | deferred |
| BL-002 | 2026-09-23 | `owner_runtime_state` table — `supervisor/infra/repositories/owner_state/repo.py`, `supervisor/infra/repositories/cleanup_checkpoint/repo.py`, `transport/dialogs/infra/repositories/repo.py` | Four repositories write to the same `owner_runtime_state` JSON blob via optimistic concurrency (read-modify-write, 5 retries). Each writer must manually preserve keys of all other writers; a new writer that forgets this silently overwrites data. Under concurrent access, retries add latency. Pre-existing since before the repository split (was in `SqlRuntimeStore`). | A task explicitly covers splitting `owner_runtime_state` into per-concern columns or separate tables, or replacing optimistic concurrency with row-level locks. | deferred |
