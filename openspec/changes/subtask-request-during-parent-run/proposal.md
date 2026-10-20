## Why

`schedule_task` writes a `requested` row and puts the scheduling request into the run's `pending_events`.
Those events leave the run only with its result, so a child task starts after its parent has finished. A
parent that waits for its child inside the same run (the AIFA symbiosis deployment: the planner calls
`schedule_task`, then a long-polling tool) waits for work that cannot start until the wait ends.

## What Changes

- `ToolEvents` gains `request_subtask(request) -> bool`: hand a persisted subtask request to the
  supervisor now. Default `False` (nothing to hand it to).
- `PublishedToolEvents` publishes the request on the run's `IAgentEventPublisher`; a refusing transport
  gives `False`.
- `schedule_task` calls it after the `requested` row is written, for a run that is not a durable job;
  accepted → the request is removed from `pending_events` (delivery stays at most once). Otherwise the
  request leaves with the run result exactly as before.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `task-lifecycle`: a new requirement states when a child's scheduling request reaches the supervisor.

## Impact

- `new_core/tools/interfaces.py`, `ouroboros/tools/events.py`, `ouroboros/tools/service/orchestration.py`.
- A child can occupy a worker slot while its parent runs; a deployment that waits for children needs
  `OUROBOROS_WORKER_SLOTS >= 2`. Behaviour of durable jobs, of a context without a publisher, and of the
  stop tree is unchanged.
