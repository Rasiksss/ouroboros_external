---
paths:
  - "ouroboros/scheduling/**"
  - "ouroboros/tools/scheduling.py"
---

# Scheduler — read before editing

How dispatch works and what the rows/indexes are for: `docs/ARCHITECTURE.md` §Agent loop and tools, §Data layout.

| Concern | Owner |
|---|---|
| Recurrence math, validation, timestamps | `ouroboros/scheduling/core.py` |
| Lifecycle the tools drive; reads `ctx.scope` | `ouroboros/scheduling/service.py` |
| Dispatch of due slots; delivery via injected callables | `ouroboros/scheduling/dispatch.py` |
| Rows, revision CAS, slot claim, due query | `ouroboros/scheduling/store.py` |
| The scheduling tools (discoverable; `SKILL.md` tells the model to `enable_tools`) | `ouroboros/tools/scheduling.py` + rows in `ouroboros/tools/catalog.py` |
| Dispatch worker thread, `OUROBOROS_SCHEDULER_*` knobs | `supervisor/runtime.py` |
| Prompt-surface rules | `skills/tasks-scheduler/SKILL.md` |

**Rules:**

- This package imports no supervisor and no transport — `run_supervisor` hands the dispatcher its delivery callables.
- `ScheduleStore` (tool-facing, takes a `scope`) and `DispatchStore` (tick's, takes none) are separate types — serving a tool from the second would let one owner act on another's row (`tests/scheduling/test_import_boundary.py`).
- The tool-facing surface is keyed by conversation as well as owner+agent; the tick is not. Isolation lives in the SQL statement, never in a check above it.
- `move_schedule_here` re-addresses delivery inside one conversation — widening it back would be a cross-peer capability.
- Dispatch has two permanent refusals: an unusable delivery chat (absent or synthetic), and a retired conversation (read through `ChatRegistryStore`). Absence is deliverable — the registry is the browser surface's alone.
- Every taken slot writes an outcome; every due row leaves the due query. `permanent` on the receipt separates a broken row from a failed publish.
- A live claim hides its slot from the due scan; a running pass keeps its own claims fresh. Feed `refresh_claims` exactly the slots the pass still owes.
- A row this process cannot decrypt is not corrupt — it is left for a peer with the key, never parked. Parking is fleet-wide, so one pod without the key would stop every owner's schedules.
- The scheduling tools carry `safety_policy="skip"`.
- `send_with_budget` returns whether a transport took the message and raises nothing — the composed callable raises on `False`.
- The delivery hint is part of `definition_fingerprint` on purpose.
