# agent-runtime Specification

## Purpose
The claim loop, slot management, and cooperative cancellation that the agent runtime owns.
## Requirements
### Requirement: Executing tasks is its own component, and the supervisor does not import it

Everything that claims a task and runs it SHALL live in one package — the slots, the agent
construction, the per-run stop signals, the drain. No module under `supervisor/` SHALL import that
package, the agent package, or any module that constructs an agent, statically or lazily. The
dependency SHALL point one way: the runtime and the composition root may name the supervisor's
durable store and its queue; the supervisor SHALL NOT name the runtime.

An import graph is the only form of this claim that can be checked. While the supervisor imports the
agent, "the supervisor does not know how tasks are executed" is a sentence about intent.

The supervisor SHALL NOT hold a reference to the object that runs tasks, SHALL NOT ask it to claim,
and SHALL NOT ask it about its health. What the supervisor does with a task after enqueuing it SHALL
be expressed as reads and writes of the task record.

The independence SHALL hold for reads as well as for imports. The scaffolding a run executes within
SHALL read the supervisor's durable state through the composed runtime store, by the run's own
scope, and SHALL NOT reach it through a registry the supervisor fills in its own process. A read
that resolves through such a registry answers only where a supervisor was composed, so it degrades
to nothing in a composition that runs tasks and no supervisor — silently, because the failure is
indistinguishable from an absent value.

#### Scenario: The supervisor's imports name no executor and no agent

- **WHEN** every module under the supervisor package is parsed and its imports inspected
- **THEN** none names the agent package or the runtime package as an import root

#### Scenario: The supervisor's loop neither claims nor assigns

- **WHEN** one pass of the supervisor's loop is inspected
- **THEN** it performs no claim, starts no run, and touches no slot

#### Scenario: The runtime does not import the supervisor's loop

- **WHEN** the runtime package's imports are inspected
- **THEN** it names the durable store and the queue it claims from
- **AND** it names neither the supervisor loop nor its event handlers

#### Scenario: The run scaffolding reads runtime state through the store

- **WHEN** the module that stamps a run's trace is parsed and its imports inspected
- **THEN** it names no supervisor module, and the session it stamps is read through the composed store

#### Scenario: Runtime state is readable without a supervisor in the process

- **WHEN** a task runs in a process that composed no supervisor
- **THEN** the run reads the owner's runtime state through the composed store and gets the recorded value

### Requirement: The runtime claims its own work and learns of it by polling

The runtime SHALL claim tasks for itself, on its own schedule. It SHALL poll the claim on a bounded
interval, and that poll SHALL be sufficient on its own: a task that is claimable SHALL eventually be
claimed with no notification from anyone.

A wake-up signal MAY exist to shorten the wait and SHALL NOT be required for correctness. A missed
or undeliverable notification SHALL cost latency and SHALL NOT strand a claimable task. This is what
lets the signal be a process-local event today and a shared backend later without the guarantee
changing.

When a run finishes, the runtime SHALL re-check for claimable work immediately rather than waiting
out the interval. That owner's next task became claimable at the moment the previous one closed and
not before, because the claim's one-run-per-owner predicate was hiding it until then.

#### Scenario: A queued task starts with no notification at all

- **WHEN** a task is enqueued and no wake-up signal reaches the runtime
- **THEN** the task is claimed and started within the polling interval

#### Scenario: A dropped wake-up costs latency only

- **WHEN** the wake-up signal for an enqueued task is lost
- **THEN** the task still starts, no later than the polling interval allows

#### Scenario: The next task for an owner starts without waiting out the interval

- **WHEN** an owner's running task closes and another of their tasks is queued
- **THEN** the queued one is claimed without waiting a full polling interval

### Requirement: The runtime holds its own identity on the records it claims

The identity stamped on a claimed record SHALL name the component that is running the task. It SHALL
be minted by the runtime, and the runtime SHALL be the only component that stamps it.

A worker id names a lane inside an instance and is not an address; the identity on the record is what
lets a stop, a release or a reclaim reason about where a run actually is. Once the supervisor no
longer runs anything, its process identity on a record would name a component that never held the
work.

Handing back runs on shutdown SHALL belong to the runtime, addressing only records that name its own
identity. Reclaiming runs whose lease went stale SHALL remain the supervisor's and SHALL continue to
address records by staleness rather than by whose they are — that is the property that lets the sweep
outlive any runtime, which is why it belongs on the other side of the split.

#### Scenario: A claimed record names the runtime that took it

- **WHEN** a task is claimed
- **THEN** the record's runtime identity is the runtime's, not the supervisor's

#### Scenario: A departing runtime hands back only its own runs

- **WHEN** a runtime shuts down while holding runs
- **THEN** it releases the records naming its own identity
- **AND** it leaves another runtime's running records and every queued record untouched

#### Scenario: The sweep still ends a run whose runtime vanished

- **WHEN** a runtime dies without releasing its runs and their leases go stale
- **THEN** the supervisor's sweep ends and requeues them as before
- **AND** it needed no reference to the runtime that held them

### Requirement: Runs of different owners proceed concurrently within one runtime instance

One runtime instance SHALL run several tasks at once, each in its own thread, and tasks belonging to
different owners SHALL make progress independently of one another. A slow or stuck run for one owner
SHALL NOT delay the start of a run for another.

At most one task per owner SHALL be running across the deployment. The runtime SHALL NOT implement
that rule: it is a predicate in the statement that claims a task, and a runtime that also tracked it
in memory would hold a second answer that is wrong the moment there are two runtimes.

#### Scenario: Two owners run at once

- **WHEN** tasks for two owners are queued and the runtime has free slots
- **THEN** both run concurrently

#### Scenario: One owner's second task waits without blocking anyone else

- **WHEN** an owner has a running task and another of their tasks is queued
- **THEN** that task waits
- **AND** a third owner's queued task is claimed and started meanwhile

#### Scenario: The runtime keeps no per-owner occupancy map

- **WHEN** the runtime's state is inspected while runs are in flight
- **THEN** nothing in it records which owners are currently running

### Requirement: A worker slot stays claimed until its thread stops or is declared abandoned

Slots belong to the runtime that holds them. No other component SHALL clear, inspect for release, or
start work into a slot.

A slot SHALL NOT be cleared at kill time. It SHALL be released by the normal completion path once its
future finishes, so no new task is claimed into a slot whose body has not returned. Consuming a run's
report SHALL NOT release a slot: the report arrives while the future is still live, and the component
consuming it does not hold slots at all.

If the future has not completed within a bounded grace period after the stop signal was set, the slot
SHALL be released anyway and a `task_abandoned` runtime event SHALL be recorded carrying the task id
and the elapsed time.

A released slot SHALL NOT have to replace anything shared with the body it lost. Where each run holds
its own agent, a leaked thread holds only its own objects, and the next task in that slot starts on a
runtime the leak cannot reach. No path SHALL keep a shared per-run object alive in order to be
replaced later.

#### Scenario: No task is claimed into a slot whose body has not returned

- **WHEN** a task has been killed by the hard timeout and its body has not yet returned
- **THEN** the slot remains claimed
- **AND** no further task is claimed into that slot

#### Scenario: Other slots keep running while one is stuck

- **WHEN** one slot's body has been killed and has not returned
- **THEN** the remaining slots continue claiming and running

#### Scenario: A body that ignores the stop signal is reported

- **WHEN** the grace period after a stop signal elapses and the future is still running
- **THEN** the slot is released
- **AND** a `task_abandoned` event is recorded with the task id and elapsed time
- **AND** nothing shared has to be replaced, because the leaked body holds only its own objects

#### Scenario: A body that stops promptly frees its slot through the normal path

- **WHEN** a stopped body returns before the grace period elapses
- **THEN** the slot is released by the usual future-completion handling
- **AND** no `task_abandoned` event is recorded

#### Scenario: Consuming a run's report releases nothing

- **WHEN** the component consuming a run's reports handles the report that the run finished
- **THEN** no slot is released by that handling
- **AND** that component holds no slot to release

### Requirement: The runtime is started, supervised and stopped as a component of its own

The composition root SHALL start the runtime as its own unit of execution, restart it on a crash up
to a bounded number of consecutive failures, and stop it on shutdown. Its failure SHALL be reportable
separately from the supervisor's: a dead runtime and a dead supervisor are different outages and lead
to different actions.

Stopping SHALL be a bounded drain — stop claiming first, ask every held run to stop, wait out a
deadline, then release what remains — and SHALL be the runtime's own operation rather than something
performed on it from outside.

Nothing in how the runtime is started SHALL assume it shares a process with the supervisor. It SHALL
reach the supervisor's durable state only through the store, and whoever consumes its reports only
through the reporting seam.

#### Scenario: The runtime is restarted after a crash

- **WHEN** the runtime's loop raises
- **THEN** it is restarted, up to the bounded number of consecutive failures
- **AND** exceeding that bound is reported rather than retried silently

#### Scenario: A dead runtime is distinguishable from a dead supervisor

- **WHEN** the runtime has given up and the supervisor is healthy
- **THEN** the reported error names the runtime

#### Scenario: Draining stops claiming before it waits

- **WHEN** the runtime is asked to stop while holding runs
- **THEN** it claims nothing further from that moment
- **AND** it asks each held run to stop, waits out the deadline, and releases what is still held

### Requirement: Stop notices address local cancellation bookkeeping only

An agent runtime SHALL consume stop notices through an injected interface and SHALL set the
process-local cancellation event only for a named task currently held by that same `runtime_id`.
The slot map SHALL remain process-local bookkeeping and SHALL NOT decide whether a run exists, who
owns it, or which runtime holds it; those decisions remain facts read from the task record.

The listener SHALL be owned by the runtime lifecycle, use a bounded poll, collect transport errors,
and stop when the runtime loop exits.

#### Scenario: A matching notice sets one signal

- **WHEN** a runtime receives a notice naming its identity and one of its held task ids
- **THEN** that task's local cancellation event is set
- **AND** other held tasks remain running

#### Scenario: Listener failure preserves execution correctness

- **WHEN** the notification listener cannot read its transport
- **THEN** the runtime continues executing
- **AND** the durable store signal remains observable through polling

