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

The rule SHALL be enforced by the writing statement rather than by a check performed on a record read
beforehand, and the caller SHALL learn the outcome from whether the write applied. A guard evaluated
over a snapshot holds only while one thread writes; the same guard as a predicate holds however many
processes write, which is what makes the rule survive a second instance rather than quietly stop
meaning anything.

Re-writing the *same* terminal status SHALL still apply, so the call that produced an outcome can
fill in its result and counters afterwards.

That carve-out means the absorbing rule SHALL NOT be relied on to refuse a *repeat* of an outcome. A
caller whose next step is correct only when this caller performed the transition SHALL name the
status it is transitioning *from* in its own predicate, and SHALL learn from that write whether it
applied. Inferring it from the status of the returned record cannot work: a repeat satisfies the
same-status carve-out, so the record reads exactly as it would have had this caller closed it. The
carve-out itself SHALL stay — removing it would refuse a late body recording its own result against
the status it already wrote.

The terminal set SHALL have exactly one Python definition, exported from
`ouroboros/storage/task_results.py` and imported by the store rather than restated. Where the same
set appears inside SQL it SHALL be bound from that definition rather than written out a second time.

Full-row writes SHALL NOT be a second, unguarded door onto records already in flight: they are
confined to the paths that create a record.

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

#### Scenario: Two processes cannot both close one record

- **WHEN** one instance's timeout enforcer and another instance's task body attempt terminal
  transitions on the same record at once
- **THEN** exactly one applies
- **AND** the other returns the applied record rather than overwriting it

#### Scenario: Transitions out of an active status still work

- **WHEN** a task in `queued`, `scheduled`, `interrupted`, or `running` is completed, failed, or
  cancelled
- **THEN** the transition is applied as before

#### Scenario: Re-writing the same terminal status is not blocked

- **WHEN** `update_task_status` is called with the status the record already holds
- **THEN** the write proceeds, so a terminal record's result and counters can still be filled in by
  the call that produced them

#### Scenario: A repeated outcome is refused by the caller's own predicate

- **WHEN** two instances each attempt to end the same `running` record with the same terminal status
- **THEN** the write that names `running` as the status it transitions from applies for exactly one
  of them
- **AND** the other learns that its write applied to no row
- **AND** neither learns this from the status of the record it reads back

### Requirement: `waiting_confirmation` is an active status of this runtime's own tasks

A record parked on an owner's confirmation SHALL count as active work wherever the runtime asks what
is still in flight: the owner's active-job limit, the job listing, the retention sweep that clears the
buffers of finished work, and the public status a client is shown. A parked run is going to continue,
and treating it as finished would let the owner exceed the limit, clear the scratchpad the resumed run
reads, or show the job as failed.

Only the queue's claim SHALL remain closed to it: a parked record is queued first and claimed after,
never claimed where it stands.

#### Scenario: A parked job counts against the owner's active jobs

- **WHEN** an owner's job is parked on a confirmation
- **THEN** admitting their next root job counts it as active

#### Scenario: A parked job is not reported as failed

- **WHEN** a client reads the public status of a parked job
- **THEN** it reads as running rather than failed

#### Scenario: The retention sweep leaves a parked job's buffers alone

- **WHEN** the sweep looks for jobs whose latest record is no longer active
- **THEN** a parked job is not among them

### Requirement: A heartbeat records liveness and never decides status

`heartbeat_task` SHALL advance `heartbeat_at` only while the stored record is `running`. For any
other status it SHALL be a no-op. It SHALL NOT drive a status transition in either direction. That
rule SHALL live inside `update_task_status`, beside the read and the terminal guard it belongs with,
and `heartbeat_task` SHALL return the record it read so one heartbeat costs one read.

The write SHALL be made by the thread running the task, not by another component consuming an event
the run emitted. A heartbeat is the run's own claim to be alive, and a claim relayed through a
component that may be a different process stops being evidence of anything the moment it is one.

Forwarding the heartbeat to live logs is independent of the store write and SHALL continue
unconditionally — it is observability, and it may travel by event.

#### Scenario: Heartbeat on a running task advances heartbeat_at

- **WHEN** a heartbeat arrives for a task whose status is `running`
- **THEN** `heartbeat_at` is set to now
- **AND** the status stays `running`

#### Scenario: Heartbeat on a queued task does not start it

- **WHEN** a heartbeat arrives for a task whose status is `queued`, `scheduled`, `interrupted`, or
  `waiting_confirmation`
- **THEN** the record is unchanged

#### Scenario: The running thread writes its own heartbeat

- **WHEN** a task body is running
- **THEN** the store write that advances `heartbeat_at` is made on that body's own path
- **AND** no other component's dispatch loop stands between the body and the store

#### Scenario: Heartbeat forwarding to live logs is unaffected

- **WHEN** a heartbeat arrives for a task in any status
- **THEN** the heartbeat is still pushed to the live log bridge

### Requirement: A hard-timeout requeue spends an attempt on the record it retries

When `enforce_task_timeouts` fails an overdue task and enqueues a retry, it SHALL increment
`attempt` on the original record in the same write that fails it. The retry cap
(`QUEUE_MAX_RETRIES`) SHALL therefore be evaluated against a counter that the requeue actually
advances.

That write SHALL be conditional on the record still being `running`, and everything the sweep does
after it — enqueueing the clone, recording the `task_hard_timeout` event, telling the owner the task
was killed — SHALL happen only when the write applied. The sweep acts on the transition it
performed, not on the listing it read: a listing is a hint, and between reading it and writing, the
body may have closed the record and another instance's sweep may have killed it.

One hung task SHALL produce at most `QUEUE_MAX_RETRIES` clones over its whole lifetime, regardless
of how many times its record is observed as `running` and overdue, and regardless of how many
instances observe it. The ceiling bounds the deployment, not one process.

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

#### Scenario: Two instances sweeping one overdue task retry it once

- **WHEN** two instances each list the same overdue `running` record before either writes, and both
  proceed to kill it
- **THEN** exactly one clone is enqueued
- **AND** exactly one `task_hard_timeout` event is recorded
- **AND** the second instance's write applies to no row, so it neither retries nor announces

#### Scenario: A body that closes the record first is not killed

- **WHEN** the body reaches a terminal status between the sweep's listing and its write
- **THEN** the sweep's write applies to no row
- **AND** no clone is enqueued and no kill is announced

### Requirement: Timeout notices reach the owner through the same outbound door as every other supervisor message

`TaskQueue` SHALL send the soft-timeout and hard-timeout notices through
`message_bus.send_with_budget`, the outbound door that publishes onto the outbox where the
transport serving the notice's `source` reads it, rather than owning a sender of its own. A notice
that cannot be delivered SHALL be logged and SHALL NOT abort the sweep of the remaining overdue
tasks.

Each notice SHALL be hung on a durable write that this sweep performed: the hard-timeout notice on
the conditional transition that killed the record, and the soft-timeout notice on a conditional
claim of the right to announce it — a write that applies for exactly one caller and reports whether
it applied. A flag read from a listing SHALL NOT be what decides that a notice goes out; several
instances list the same record before any of them writes, and every one of them then reads the flag
unset.

The claim SHALL be a predicate in the announcing statement, over a column of its own. It SHALL NOT
be a value merged into a JSON blob by a read-modify-write, because that merge is last-writer-wins and
cannot refuse a second announcer. A write that rewrites the whole record SHALL NOT be able to clear
the claim, so a claim recorded between another writer's read and its write survives.

The notice SHALL be sent after the write is confirmed and SHALL NOT be enclosed in its transaction.
The outbound door performs writes and a publish of its own and may fail, and a notice inside the
transition's transaction would either undo a kill that did happen or be swallowed, which the
transaction would not have improved.

Delivery is therefore at-most-once: if the process dies between the confirmed write and the notice,
the owner is not told, and no later sweep on any instance re-sends it, because the write that would
have announced it has already applied. This is chosen over the alternative — the duplicate notice
this rule exists to prevent — and closing the window is not in scope.

A timeout notice is addressed by the overdue record's own stored scope and by nothing else: not by
a scope minted from the task's source, and not by the instance's stored owner, which could stand in
only while every record belonged to the same person. Where the record names no scope and its own
columns recover none, no notice SHALL be sent for it — and the claim that records a notice as sent
SHALL NOT be taken either, because a claim spent on an announcement nobody received would silence
every later sweep as well.

Where the record names a scope but no delivery source, the notice SHALL carry the declared `unknown`
source and the sweep SHALL log once that it has no deliverable source, rather than leave a lenient
surface lookup to absorb it silently.

#### Scenario: A hard-timeout notice is delivered to the transport

- **WHEN** a task is killed by the hard timeout and a route chat is known
- **THEN** the `🛑 Hard-timeout` message is delivered through the same door that carries messages
  emitted from `supervisor/events.py`

#### Scenario: A soft-timeout notice is delivered to the transport

- **WHEN** a task passes the soft timeout for the first time
- **THEN** the `⏱️` notice is delivered through that same door
- **AND** it is sent only once per task, across every instance sweeping it

#### Scenario: An undeliverable notice does not stop the sweep

- **WHEN** the outbound door raises while a timeout notice is being delivered
- **THEN** the overdue task is still failed and its retry still enqueued

#### Scenario: A record that names no scope is not announced

- **WHEN** an overdue record's stored input names no scope and its own columns recover none
- **THEN** no timeout notice is sent for it
- **AND** the instance's stored owner is not used as a stand-in
- **AND** the soft-notice claim is left untaken, so no later sweep treats it as announced

#### Scenario: A record with no delivery source is logged rather than absorbed

- **WHEN** an overdue record names a scope but no source
- **THEN** the notice is addressed with that scope and carries the `unknown` source
- **AND** the sweep logs once that the notice has no deliverable source

#### Scenario: Two instances past the soft timeout notify once

- **WHEN** two instances list the same record before either writes, and both find it past its soft
  timeout with no notice claimed
- **THEN** the claim applies for exactly one of them
- **AND** the owner receives exactly one `⏱️` notice

#### Scenario: A record that stops running before the claim is not announced

- **WHEN** a record leaves `running` between the sweep's listing and its claim
- **THEN** the claim applies to no row
- **AND** no soft-timeout notice is sent for it

#### Scenario: A rewritten record keeps its claim

- **WHEN** a full-record write lands on a task whose soft-timeout notice has already been claimed
- **THEN** the claim is still recorded afterwards
- **AND** a later sweep does not announce the task again

### Requirement: A killed or cancelled task is stopped cooperatively, not by cancelling a future

Ending a running task SHALL be expressed as a per-task stop signal that the body observes, not as a
call to `Future.cancel()` on a future that has already started. The signal SHALL be set by every path
that decides a running task is over: the hard-timeout enforcer, the stale-lease reclaimer, the
departing instance's release, and the user- or agent-initiated cancel.

The signal SHALL be an interface — `CancellationSignal`, with `is_stopped()` and `request_stop()` —
and never a bare callable. It SHALL have an implementation that reads state shared across instances,
so that a stop asked for anywhere reaches the run wherever it is held; observers SHALL NOT change
between implementations.

The signal SHALL NOT be reachable only through the worker slot. A slot is one process's bookkeeping
and cannot be indexed from another instance, so a path that decides a run is over SHALL address the
run rather than a lane. A slot MAY still carry the signal for the run it holds.

`Future.cancel()` MAY still be called first, as the correct stop for a future that has not started.
It SHALL NOT be treated as sufficient.

The stop SHALL be set only after the task's record has been written to its terminal status, so that
a body returning in that window finds its task already closed.

A body SHALL be handed the signal belonging to the claim that started it, read when the run is
submitted rather than from the slot inside the worker thread, so a slot released and re-claimed
meanwhile cannot switch a running body onto another task's signal.

Executor shutdown SHALL end running tasks through those same per-task signals rather than a separate
executor-wide one. A lane that observes no stop signal at all SHALL remain covered by the slot's
grace period rather than being left with no bound.

A stopped body SHALL release nothing beyond its own slot. Where each run holds its own agent there is
no shared execution lock for a body to hold or release, and the next task's ability to start SHALL
NOT depend on a stopping body letting go of anything.

#### Scenario: Hard timeout sets the stop signal

- **WHEN** a running task exceeds the hard timeout
- **THEN** its stop signal is set
- **AND** the task record is failed as before
- **AND** the record is already terminal at the moment the signal is set

#### Scenario: A stop asked for on another instance reaches the run

- **WHEN** a stop is requested for a task held by a different instance
- **THEN** the run's signal reports a stop
- **AND** no caller needed a reference to the object running it

#### Scenario: A reused slot does not switch a running body's signal

- **WHEN** a body is submitted for a claim and its slot is later released and claimed again
- **THEN** the running body still observes the signal of its own claim

#### Scenario: Cancelling a running task sets the stop signal

- **WHEN** a running task is cancelled by the user or by the agent
- **THEN** its stop signal is set
- **AND** the task record is cancelled as before

#### Scenario: A stopped body blocks nothing

- **WHEN** a task's body observes the stop signal and returns
- **THEN** no shared lock is released, because none was held
- **AND** the next queued task can run regardless of when that body returned

#### Scenario: A background lane with a stop signal observes the per-task one

- **WHEN** a `context_consolidation` task is killed by the hard timeout
- **THEN** its run observes the stop at its next check and returns

#### Scenario: Executor shutdown stops every running body

- **WHEN** the executor is shut down while tasks are running
- **THEN** each task's own stop signal reports a stop
- **AND** each body observes it even though its slot has already been released

#### Scenario: A lane with no stop signal is still bounded

- **WHEN** a `memory_distillation` task is killed by the hard timeout and its body does not observe
  any stop signal
- **THEN** the slot's grace period releases the slot and the abandonment is reported

### Requirement: A stop reaches a record that has no run

A stop asked for a record in `waiting_confirmation` SHALL be recorded as it is for a running one, and
the record SHALL then be queued so that the run started for it observes the stop and ends. A parked
record has no thread to observe a cooperative stop, so recording the intent alone would leave it
parked forever; closing the record outright would leave the workspace the parked run kept for its
answer (`tool-confirmation`) with no run to remove it. The job stop SHALL queue a parked record the
same way, rather than cancel it as it cancels a record that never started.

The owner's restart SHALL cancel a parked record as it cancels a queued one. A restart means "do not
resume mine", and a record left in either status is claimed again the moment the process is back.

#### Scenario: A stop on a parked record queues it

- **WHEN** the owner stops a task that is parked on a confirmation
- **THEN** the stop is recorded and the record is queued

#### Scenario: A job stop queues a parked record

- **WHEN** the owner's jobs are stopped and one of them is parked
- **THEN** that record is queued with the stop recorded, as it is when the task itself is stopped

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

### Requirement: A run whose instance is gone is ended by its lease, not by its timeout

A record in `running` whose heartbeat has gone stale SHALL be ended, whatever its runtime so far. The
hard timeout bounds a run that is alive and too slow; it SHALL NOT be the only thing that ends a run
whose instance died, because a lost process is not a slow one and a user waiting behind it should not
wait out a timeout meant for work that is still happening.

The two outcomes SHALL be distinguishable on the record and in what the owner is told: one says the
task took too long, the other says the instance running it stopped reporting.

A reclaimed run SHALL be requeued through the same mechanism the hard-timeout requeue uses, and SHALL
spend an attempt on the chain it retries, so one retry ceiling bounds both kinds of requeue together.

#### Scenario: A lost instance's run ends on its lease

- **WHEN** an instance dies holding a running record well before the hard timeout would fire
- **THEN** the record is ended once the lease goes stale
- **AND** a clone is enqueued and the chain's attempt counter advanced
- **AND** the owner is told the instance stopped reporting, not that the task took too long

#### Scenario: Reclaims and timeouts share one retry ceiling

- **WHEN** a chain has already spent its attempts through hard-timeout requeues
- **THEN** a reclaim of that chain does not enqueue a further clone

#### Scenario: A slow but live run is not reclaimed

- **WHEN** a run is still writing heartbeats and has not exceeded the hard timeout
- **THEN** no reclaim applies to it

### Requirement: A shutdown is a release, not a terminal outcome

Executor shutdown SHALL NOT be a path that writes records terminal on behalf of the deployment. It
SHALL release the runs the departing instance holds — requeueing them, spending an attempt as the
other requeue paths do — and SHALL leave every other record untouched, whether it is queued or held
by another instance.

There are now three ways a run ends before its body finishes, and they SHALL remain distinguishable
on the record and in what the owner is told: a hard timeout says the task took too long, a reclaim
says the instance running it stopped reporting, and a release says the instance running it went away
on purpose. Only the first is a failure.

#### Scenario: Shutdown requeues the departing instance's runs

- **WHEN** an instance shuts down holding a running record
- **THEN** the record is requeued and the attempt counter advanced
- **AND** its outcome names a release rather than a failure or a timeout

#### Scenario: Shutdown does not close records it does not hold

- **WHEN** an instance shuts down
- **THEN** no queued record and no other instance's running record changes status

### Requirement: A task's terminal status has exactly one author, and it is the body that ran it

The transition of a task record to a terminal status SHALL be written by the run that produced the
outcome, synchronously, before that run reports anything about it. No other component SHALL write a
task's terminal status on the strength of a report that the task finished. A component consuming such
a report MAY record it for display and observability, and SHALL perform no status transition.

The reason is the one already recorded for the lease: a fact written by a dispatch loop is a fact
recorded by a component that may be a different process from the one that observed it. A terminal
status is such a fact. It is also already written this way — the body closes its own record on both
the success and the crash paths — so what this requirement removes is a second writer, not an absence
of one.

A second writer whose writes are absorbed by the first is the dangerous case rather than the harmless
one: it appears to work for exactly as long as it does not matter, and the day it disagrees with the
first writer is the day nobody is watching it.

Everything the removed write uniquely carried — the token counts and the round count — SHALL be
written by the body's own write, which holds those totals at the moment it closes the record.

#### Scenario: The record is terminal before the run reports it

- **WHEN** a run completes a task
- **THEN** the record is already terminal when the report that it finished is published

#### Scenario: Handling the report writes no status

- **WHEN** the report that a task finished is consumed
- **THEN** no status transition is attempted for that task
- **AND** the observability record for it is still written

#### Scenario: The counters survive the removal

- **WHEN** a task completes
- **THEN** its record carries the token counts and round count recorded by the run itself

#### Scenario: A crashed run still closes its own record

- **WHEN** a run raises before producing a result
- **THEN** the record is failed by the runtime that held it
- **AND** no consumer of its reports is required for that to happen

### Requirement: Stopping a job atomically closes its current task tree

A job stop SHALL select the current root attempt and recursively include every descendant linked by
`parent_task_id` in the same database transaction. Descendants in `requested`, `queued`, `scheduled`,
or `interrupted` without saved confirmation state SHALL become `cancelled`. A descendant in
`waiting_confirmation`, or queued/interrupted with saved confirmation state, SHALL be queued with
`stop_requested_at` so its run performs terminal cleanup without executing an approved tool. Repeated
stops SHALL preserve this cleanup path. Running records SHALL retain
`running` status and receive `stop_requested_at`. Terminal records SHALL remain unchanged, but SHALL
NOT prevent traversal to an active descendant. Another root under the same owner SHALL remain
untouched. If a subtask call already in progress persists a requested child after the tree update,
the stopped parent's terminal path SHALL cancel that row and suppress its scheduling event.

The initial `requested` row written by `schedule_task` SHALL already carry its parent in the
`parent_task_id` column, so tree membership does not depend on supervisor acceptance.

#### Scenario: A mixed task tree is stopped

- **WHEN** a running root has waiting and running descendants at multiple depths
- **THEN** ordinary waiting descendants are cancelled and confirmation waits are queued with stop intent
- **AND** every running record receives durable stop intent
- **AND** the changes commit atomically

#### Scenario: A terminal parent does not hide an active grandchild

- **WHEN** an active descendant is linked through a terminal intermediate task
- **THEN** the active descendant is still cancelled

#### Scenario: An unrelated root is isolated

- **WHEN** the owner has another root outside the selected tree
- **THEN** that root and all of its descendants remain unchanged

#### Scenario: Stop all includes an active descendant of a completed root

- **WHEN** an unqualified durable stop finds a completed current root with a waiting or running descendant
- **THEN** that tree is selected as active
- **AND** the descendant is stopped normally

#### Scenario: Child persistence loses the terminal race

- **WHEN** a subtask call persists its requested child after the parent tree stop committed
- **THEN** terminal emission cancels the child
- **AND** no scheduling event for that child leaves the stopped run

#### Scenario: A completed parent's scheduling event is still pending

- **WHEN** a parent has completed and its persisted child request has not yet been accepted
- **AND** a named stop selects the parent's job
- **THEN** the requested child is cancelled with the tree
- **AND** later acceptance of the scheduling event cannot make the child runnable

#### Scenario: Stop all finds a descendant of an older root attempt

- **WHEN** the current root attempt has completed but an older attempt has an active descendant
- **THEN** an unqualified durable stop selects that logical job and stops the descendant
- **AND** the returned root is the current attempt and is counted once

#### Scenario: A stopped confirmation retains its cleanup run

- **WHEN** a tree stop reaches a descendant parked on an owner confirmation
- **OR** the owner already answered and that descendant is queued with its saved confirmation state
- **THEN** the descendant remains claimable with durable stop intent
- **AND** repeating the stop does not cancel its cleanup run
- **AND** a late approval cannot authorize another tool execution

#### Scenario: A foreground stop reaches a confirmed root before its next claim

- **WHEN** durable jobs are disabled and an owner's reply has queued a root with saved confirmation state
- **AND** the owner issues `/stop` before the runtime claims that continuation
- **THEN** the root receives durable stop intent and remains queued for terminal cleanup
- **AND** an unrelated queued task without confirmation state remains unchanged
