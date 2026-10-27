## Why

A planner child can be rejected as a semantic duplicate of its running parent. The rejection
notification has no task ID, so clients waiting for that child never receive its terminal result.

## What Changes

- Exclude the scheduling task's same-owner ancestor chain from semantic duplicate candidates.
- Preserve duplicate detection against siblings and unrelated active tasks.
- Publish a rejected child's failure with its task ID and original transport, even when admission
  acknowledgements are suppressed. Keep the stored status `rejected_duplicate`.

## Capabilities

### Modified Capabilities

- `task-lifecycle`: ancestor-aware subtask admission and correlated rejection outcomes.

## Impact

`supervisor/events.py` and supervisor regression tests. No configuration or identity changes.
