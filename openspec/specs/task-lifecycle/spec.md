# task-lifecycle Specification

## Purpose

The supervisor-side lifecycle of a task *record*: which status transitions the runtime store
accepts, how a heartbeat interacts with status, how hard-timeout retries are counted and bounded,
and how timeout notices reach the owner. The governing rule is that the first terminal outcome wins
— a body the supervisor could not stop must not be able to resurrect or overwrite the record that
already buried it — and that the retry counter is advanced on the record the retry cap is meant to
bound. What a task *does* while it runs — context assembly, the loop, result emission — is not
specified here; see `task-graph` and `loop-graph`.

## Requirements

### Requirement: A terminal task status is absorbing

Once a task record reaches a terminal status (`completed`, `failed`, `cancelled`,
`rejected_duplicate`, `budget_exceeded`), the runtime store SHALL reject any supervisor-path
transition to a different status. `update_task_status` SHALL return the stored record unchanged and
log at debug level; it SHALL NOT raise. The first terminal outcome wins, together with the `result`,
`error_code`, and `finished_at` recorded with it.

The terminal set SHALL have exactly one Python definition, exported from
`ouroboros/storage/task_results.py` and imported by the store rather than restated.

This requirement covers the `update_task_status` path only. `upsert_task` remains an unguarded write
door, used by A2A ingress to re-persist whole records; that gap is deliberate and recorded in the
change's design.

#### Scenario: A heartbeat cannot resurrect a failed task

- **WHEN** a task has been failed by the hard-timeout enforcer
- **AND** the still-running body emits `task_heartbeat` for that task
- **THEN** the record's status stays `failed`
- **AND** `heartbeat_at` is not advanced
- **AND** the record does not reappear in `list_running()`

#### Scenario: A late task_done cannot overwrite a failed task

- **WHEN** a task has been failed by the hard-timeout enforcer
- **AND** the body later finishes and emits `task_done` with status `completed`
- **THEN** the record's status stays `failed`
- **AND** the `result` recorded by the hard timeout is preserved

#### Scenario: Transitions out of an active status still work

- **WHEN** a task in `queued`, `scheduled`, `interrupted`, or `running` is completed, failed, or
  cancelled
- **THEN** the transition is applied as before

#### Scenario: Re-writing the same terminal status is not blocked

- **WHEN** `update_task_status` is called with the status the record already holds
- **THEN** the write proceeds, so a terminal record's result and counters can still be filled in by
  the call that produced them

### Requirement: A heartbeat records liveness and never decides status

`heartbeat_task` SHALL advance `heartbeat_at` only while the stored record is `running`. For any
other status it SHALL be a no-op. It SHALL NOT drive a status transition in either direction. That
rule SHALL live inside `update_task_status`, beside the read and the terminal guard it belongs with,
and `heartbeat_task` SHALL return the record it read so one heartbeat costs one read.

Forwarding the heartbeat to live logs is independent of the store write and SHALL continue
unconditionally.

#### Scenario: Heartbeat on a running task advances heartbeat_at

- **WHEN** a heartbeat arrives for a task whose status is `running`
- **THEN** `heartbeat_at` is set to now
- **AND** the status stays `running`

#### Scenario: Heartbeat on a queued task does not start it

- **WHEN** a heartbeat arrives for a task whose status is `queued`, `scheduled`, `interrupted`, or
  `waiting_confirmation`
- **THEN** the record is unchanged

#### Scenario: Heartbeat forwarding to live logs is unaffected

- **WHEN** a heartbeat arrives for a task in any status
- **THEN** the heartbeat is still pushed to the live log bridge

### Requirement: A hard-timeout requeue spends an attempt on the record it retries

When `enforce_task_timeouts` fails an overdue task and enqueues a retry, it SHALL increment
`attempt` on the original record in the same write that fails it. The retry cap
(`QUEUE_MAX_RETRIES`) SHALL therefore be evaluated against a counter that the requeue actually
advances.

One hung task SHALL produce at most `QUEUE_MAX_RETRIES` clones over its whole lifetime, regardless
of how many times its record is observed as `running` and overdue.

#### Scenario: One hung body produces one clone

- **WHEN** a task outlives the hard timeout and its body keeps running and heartbeating across many
  enforcement ticks
- **THEN** exactly one retry is enqueued
- **AND** the retry carries `timeout_retry_from` pointing at the original and `attempt = 2`

#### Scenario: A resurrected record still cannot be retried again

- **WHEN** a hard-timed-out record is forced back to `running` by any means
- **AND** `enforce_task_timeouts` runs again on it
- **THEN** no additional retry is enqueued, because the record's `attempt` already exceeds
  `QUEUE_MAX_RETRIES`

#### Scenario: A retry that times out is not retried further

- **WHEN** the retry clone itself outlives the hard timeout
- **THEN** it is failed with the "retry limit exhausted" notice and no further clone is enqueued

### Requirement: Timeout notices reach the owner through the same outbound door as every other supervisor message

`TaskQueue` SHALL send the soft-timeout and hard-timeout notices through
`message_bus.send_outbound_message`, the outbound door already configured with the transport
fanout, rather than owning a sender of its own. A notice that cannot be delivered SHALL be logged
and SHALL NOT abort the sweep of the remaining overdue tasks.

#### Scenario: A hard-timeout notice is delivered to the transport

- **WHEN** a task is killed by the hard timeout and a route chat is known
- **THEN** the `🛑 Hard-timeout` message is delivered through the same door that carries messages
  emitted from `supervisor/events.py`

#### Scenario: A soft-timeout notice is delivered to the transport

- **WHEN** a task passes the soft timeout for the first time
- **THEN** the `⏱️` notice is delivered through that same door
- **AND** it is sent only once per task

#### Scenario: An undeliverable notice does not stop the sweep

- **WHEN** the outbound door raises while a timeout notice is being delivered
- **THEN** the overdue task is still failed and its retry still enqueued

### Requirement: A killed or cancelled task is stopped cooperatively, not by cancelling a future

Ending a running task SHALL be expressed as a per-task stop signal that the body observes, not as a
call to `Future.cancel()` on a future that has already started. The signal SHALL live on the worker
slot alongside the task's other per-task handles, and SHALL be set by every path that decides a
running task is over: the hard-timeout enforcer and the user- or agent-initiated cancel.

The signal SHALL be an interface — `CancellationSignal`, with `is_stopped()` and `request_stop()` —
and never a bare callable, so that the process-local implementation can be replaced by one reading
state shared across supervisor instances without any observer changing.

`Future.cancel()` MAY still be called first, as the correct stop for a future that has not started.
It SHALL NOT be treated as sufficient.

The stop SHALL be set only after the task's record has been written to its terminal status, so that
a body returning in that window finds its task already closed.

A body SHALL be handed the signal belonging to the claim that started it, read when the run is
submitted rather than from the slot inside the worker thread, so a slot released and re-claimed
meanwhile cannot switch a running body onto another task's signal.

Executor shutdown SHALL end running tasks through those same per-task signals rather than a separate
executor-wide one, so that stopping one task and shutting the whole executor down are one mechanism.
A lane that observes no stop signal at all SHALL remain covered by the slot's grace period rather
than being left with no bound.

#### Scenario: Hard timeout sets the stop signal

- **WHEN** a running task exceeds the hard timeout
- **THEN** its stop signal is set
- **AND** the task record is failed as before
- **AND** the record is already terminal at the moment the signal is set

#### Scenario: A reused slot does not switch a running body's signal

- **WHEN** a body is submitted for a claim and its slot is later released and claimed again
- **THEN** the running body still observes the signal of its own claim

#### Scenario: Cancelling a running task sets the stop signal

- **WHEN** a running task is cancelled by the user or by the agent
- **THEN** its stop signal is set
- **AND** the task record is cancelled as before

#### Scenario: A stopped body releases the agent lock

- **WHEN** a task's body observes the stop signal and returns
- **THEN** the shared agent lock is released
- **AND** the next queued task can run

#### Scenario: A background lane with a stop signal observes the per-task one

- **WHEN** a `context_consolidation` task is killed by the hard timeout
- **THEN** its run observes the stop at its next check and returns

#### Scenario: Executor shutdown stops every running body

- **WHEN** the executor is shut down while a task is running
- **THEN** that task's own stop signal reports a stop
- **AND** the body observes it even though its slot has already been released

#### Scenario: A lane with no stop signal is still bounded

- **WHEN** a `memory_distillation` task is killed by the hard timeout and its body does not observe
  any stop signal
- **THEN** the slot's grace period releases the slot and the abandonment is reported

### Requirement: A worker slot stays claimed until its thread stops or is declared abandoned

The hard-timeout enforcer SHALL NOT clear a slot's `busy_task_id` and `future` at kill time. The slot
SHALL be released by the normal completion path once the future finishes, so no new foreground task
is claimed while a stopping body still holds the shared agent lock. Handling a `task_done` event
SHALL NOT release the slot either: the event arrives while the future is still live, and releasing
there would let the next claim overwrite the future handle and hide the old body from both the
completion path and the abandonment check.

If the future has not completed within a bounded grace period after the stop signal was set, the slot
SHALL be released anyway and a `task_abandoned` runtime event SHALL be recorded carrying the task id
and the elapsed time. Releasing an abandoned slot in the lane that holds the shared agent lock SHALL
also replace that lock and the agent behind it, so the next task does not block on a lock the leaked
thread still holds. A lane that never takes the lock SHALL keep its agent.

#### Scenario: No task is claimed while a stopping body still holds the lock

- **WHEN** a task has been killed by the hard timeout and its body has not yet returned
- **THEN** the slot remains claimed
- **AND** the queue does not claim and start another foreground task into that slot

#### Scenario: A body that ignores the stop signal is reported

- **WHEN** the grace period after a stop signal elapses and the future is still running
- **THEN** the slot is released
- **AND** a `task_abandoned` event is recorded with the task id and elapsed time
- **AND** for the lane that holds the agent lock, the next task gets a fresh lock and agent

#### Scenario: A body that stops promptly frees its slot through the normal path

- **WHEN** a stopped body returns before the grace period elapses
- **THEN** the slot is released by the usual future-completion handling
- **AND** no `task_abandoned` event is recorded

### Requirement: A result produced after the record went terminal is delivered with a marker

When a task body reaches a final answer for a record that is already in a terminal status, the answer
SHALL still be delivered to the owner, prefixed with a marker naming why the task was stopped and
when. The record SHALL remain in its first terminal status, keeping the `finished_at` that closed it:
a late body records its trace, never a new completion time.

A run whose record closed on any terminal status other than `completed` SHALL NOT write a
working-memory episode and SHALL NOT enqueue a follow-up `memory_distillation`.

When the body reaches the terminal with no final answer — the case produced by honouring the stop
signal — no owner message SHALL be sent; the supervisor's own timeout or cancellation notice is the
owner-facing message for that task.

When the record is still in an active status, result delivery SHALL be unchanged.

#### Scenario: A late answer reaches the owner labelled as late

- **WHEN** a body finishes and produces an answer for a task whose record is already `failed` by hard
  timeout
- **THEN** the answer is sent to the owner prefixed with a marker naming the hard timeout and the
  time the task was stopped
- **AND** the record stays `failed` with its original result and `finished_at`

#### Scenario: A stopped run leaves no episode and no distillation

- **WHEN** a task's record closed as `cancelled`, `budget_exceeded`, or `rejected_duplicate`
- **THEN** no working-memory episode is written and no `memory_distillation` task is enqueued

#### Scenario: A cooperatively stopped task sends no answer

- **WHEN** a body observes the stop signal and reaches the terminal without a final answer
- **THEN** no owner message is sent for that task
- **AND** the owner has already received the supervisor's timeout or cancellation notice

#### Scenario: A normal task is unaffected

- **WHEN** a body finishes for a task whose record is still active
- **THEN** the answer is delivered with no marker, exactly as before
