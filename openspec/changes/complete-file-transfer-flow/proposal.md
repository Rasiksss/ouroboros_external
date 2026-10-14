## Why

Stored-file delivery still holds transfer capacity during document validation. Browser batches start ten uploads concurrently and discard refused files. Short artifact names can select an earlier task's output.

## What Changes

- Release stored-file download admission before send validation.
- Acquire upload admission before reserving a catalog record, so capacity refusals reserve neither a file nor its name.
- Resolve exact file identities and paths first, then the current task's artifact path, then an accessible unique filename.
- In ouroboros_bff, queue uploads, retry explicit capacity refusals with bounded attempts and Retry-After, retain failures for retry, and prevent sending an incomplete selection.

## Capabilities

### Modified Capabilities
- `attachment-parsing`: stored-file send validation consumes no transfer lease.
- `task-artifact-publication`: task-local fallback precedes filename matching.

## Impact

Existing file resolver, send flow, browser composer and focused regression tests. No migration, new service, or quota increase.
