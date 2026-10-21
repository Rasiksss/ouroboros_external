## Context

`_schedule_task` (`ouroboros/tools/service/orchestration.py`) appends `SubtaskRequested` to
`ctx.pending_events` before writing the `requested` row. The agent returns `list(self._pending_events)`
and the runtime publishes the list after `handle_task` (`ouroboros/agent_runtime/executor.py`). The
supervisor handler (`_handle_schedule_task`) therefore sees a child only after the parent's run ended.

## Decisions

**Publish at the end of the tool call, not at the end of the round.** The tool already holds the request
and the record it names; a round-end flush would need a new loop interface (`new_core/loop`) for no gain.
`EventSinkAdapter.ask_owner` is the precedent for an event that must not wait for the run's end.

**Record first, publish second.** The handler enqueues a child whose row `schedule_task` wrote; publishing
before the write would let the handler outrun it. The write failing leaves the old behaviour (the request
stays pending).

**A method on `ToolEvents`, not a duck-typed publisher.** The interface's own rule is one method per kind of
event; this is the fourth kind. The default returns `False`, so every other implementation and every test
context behaves as before.

**Durable jobs keep the run-result path.** `finish_detached_attempt(pending_child_ids)` closes the attempt
over the children listed in the result; moving them out would change that contract and is not needed.

**Delivery stays at most once.** Accepted → removed from `pending_events`; refused → stays. A request is
never in both.

## Risks

- A parent stopped between the record write and the publish: covered by the stop-tree requirements (the
  requested row is cancelled with the tree and the handler answers "not scheduled because its parent was
  stopped").
- A child holds a worker slot while its parent waits; the deployment sets enough slots.
