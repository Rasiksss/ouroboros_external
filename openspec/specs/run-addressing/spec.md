# run-addressing Specification

## Purpose

How a run in flight is identified, found, kept alive and stopped from anywhere in a deployment. The
stored task record is the registry: it names the instance that claimed the run, carries the lease the
running thread keeps current, and holds the stop intent any instance can write. Every addressing
decision — stopping a run, cancelling one, reporting status — is a query over records rather than a
lookup into the deciding process's memory, and a record whose lease goes stale is reclaimed exactly
once and requeued.
## Requirements
### Requirement: The record of a running task is the registry

A run in flight SHALL be found through its stored task record and through nothing else. No second
structure — in memory, in a cache, or in another store — SHALL be maintained as a parallel answer to
"who is running what". The record already carries the owner, the agent, the surface and the chat; a
second copy of that answer is a second thing to keep true.

Every addressing decision — stopping a run, cancelling one, reporting status, refusing an action
because a run is in flight — SHALL be a query over records, not a lookup into the deciding process's
memory. A process SHALL address its own runs the same way it addresses another instance's, so there
is one path and it is exercised by every deployment.

Handles that cannot leave a process — a future, a thread, a pool slot — MAY remain process-local.
They SHALL be bookkeeping for the process that owns them and SHALL NOT be consulted to answer
whether a run exists, whose it is, or where it runs.

#### Scenario: A stop finds the run through the record

- **WHEN** an owner asks to stop their running task
- **THEN** the run is located by querying records for that owner
- **AND** no code path indexes a worker slot by position to find it

#### Scenario: Addressing does not depend on locality

- **WHEN** the addressing paths are inspected
- **THEN** none branches on whether the run is held by this process
- **AND** none reads a process-local map to decide that a run exists

### Requirement: A claimed record names the instance that holds it

A record in `running` SHALL name the instance that claimed it, through a `runtime_id` that is stable
for that process's lifetime and unique among concurrently live instances. The existing integer
worker id SHALL remain a within-instance lane number and SHALL NOT be used as an address: every
instance has a lane `0`, so a lane number alone names a run only while there is one process.

`runtime_id` SHALL be minted once when the runtime starts and SHALL NOT be derived from a value that
repeats across hosts or restarts, so that two live instances cannot present the same identity and a
restarted instance cannot be mistaken for its predecessor.

#### Scenario: Two instances do not collide on a lane number

- **WHEN** two instances each claim a task onto their lane 0
- **THEN** the two records name different instances
- **AND** a query for one instance's runs returns only its own

#### Scenario: A restarted instance is not its predecessor

- **WHEN** an instance restarts and claims a task
- **THEN** its runtime identity differs from the one its previous process presented

### Requirement: Liveness is written by the process that is alive

The heartbeat that keeps a run's lease current SHALL be written by the thread running the task,
directly to the store. It SHALL NOT reach the store by way of a queue consumed by another component,
because liveness recorded by a different process than the one that is alive stops being evidence the
moment those are different processes — and reports a dead run as healthy, or a healthy one as dead,
depending on which side survived.

Forwarding a heartbeat to live logs MAY remain an event on that queue: it is observability, and
losing it costs a log line rather than a run.

#### Scenario: A run keeps its own lease current

- **WHEN** a task body is running
- **THEN** its `heartbeat_at` advances through a write made by the running thread
- **AND** no other component's dispatch loop is required for that to happen

#### Scenario: Live-log forwarding is unaffected

- **WHEN** a heartbeat is recorded
- **THEN** the live log still receives it

### Requirement: A stop is a durable intent, not a call

Asking a run to stop SHALL be recorded on its record, with a conditional write that names the record
the caller believes it is stopping. The write SHALL be refused rather than applied when the record no
longer matches — a different owner, or a status that is no longer running — so that a stop cannot
land on a record the caller never saw.

Stopping SHALL be idempotent: a second request over a record that already carries a stop SHALL leave
the first request's time in place, because the grace period runs from when stopping was first asked
for.

An owner SHALL only be able to stop their own runs. Where an operator-level stop is needed, it SHALL
be a distinct, named authority and not the absence of a check.

#### Scenario: A stop reaches a run on another instance

- **WHEN** an owner asks to stop a task claimed by a different instance
- **THEN** the intent is recorded on the task's record
- **AND** the instance holding the run observes it and ends the run

#### Scenario: One owner cannot stop another's run

- **WHEN** a stop is requested for a record belonging to a different owner
- **THEN** the write is refused and no record changes

#### Scenario: A repeated stop does not extend the grace period

- **WHEN** a stop is requested twice for the same run
- **THEN** the recorded time is the first request's

### Requirement: The stop signal reads shared state, and its latency is bounded and stated

The `CancellationSignal` a run observes SHALL have an implementation that reads the recorded stop
intent, so a run stops whichever instance asked. Observers SHALL NOT change: the interface is what
they depend on.

That implementation observes a stored fact rather than an in-process event, so a stop SHALL be
honoured within a bounded delay rather than instantly. The bound SHALL be an explicit interval, and
the check SHALL be cheap enough to run between rounds without making a run's cost depend on how long
it has been waiting to be stopped — a cached read refreshed on that interval, not a query per check.

#### Scenario: A run observes a stop recorded elsewhere

- **WHEN** a stop intent is recorded for a running task
- **THEN** the run's signal reports a stop within the stated interval

#### Scenario: Checking for a stop does not cost a query each time

- **WHEN** a run checks its signal many times inside one interval
- **THEN** the store is read at most once for that interval

### Requirement: A lease that goes stale is reclaimed exactly once

A record in `running` whose heartbeat is older than the stale interval SHALL be reclaimed: its
instance is gone, and nothing else will ever end it. Reclaim SHALL be a conditional write that names
the state the reclaimer observed, so two instances sweeping the same record cannot both act on it —
one applies and one finds nothing to apply.

A reclaimed run SHALL be requeued rather than failed, through the same requeue the hard timeout
already uses: the original record goes terminal and a clone is enqueued. The requeue SHALL spend an
attempt from the same retry chain, so that a task which kills the instance running it is bounded to
one migration instead of taking instances down one at a time across the fleet.

The stale interval SHALL have exactly one definition and one reader, and SHALL be expressed in terms
of the heartbeat period rather than standing beside it as an independent number — two constants that
must agree, and can be tuned separately, will eventually disagree. A constant declared and never read
is a bound nobody enforces.

Reclaim SHALL be distinct from the hard timeout: a hard timeout ends a run that is alive and taking
too long, and a reclaim ends a run whose instance stopped saying it exists. They SHALL be separately
observable, because one indicates a slow task and the other a lost process.

Because a lost lease is evidence that an instance stopped *reporting* and not that it stopped
*running*, requeueing makes this path at-least-once: a clone MAY run beside an original that nobody
stopped. The already-terminal original SHALL keep the outcome that closed it, so a late result from
the original cannot overwrite the clone's.

#### Scenario: A dead instance's run is reclaimed

- **WHEN** an instance dies holding a running record and the lease goes stale
- **THEN** another instance reclaims that record

#### Scenario: Two sweepers do not both reclaim

- **WHEN** two instances observe the same stale record and both attempt a reclaim
- **THEN** exactly one write applies and the other reports no change

#### Scenario: A reclaim is not reported as a timeout

- **WHEN** a record is reclaimed for a stale lease
- **THEN** the outcome names the lost lease rather than a hard timeout

#### Scenario: A reclaimed run is requeued and spends an attempt

- **WHEN** a record is reclaimed for a stale lease
- **THEN** a clone is enqueued and the original record is terminal
- **AND** the attempt counter on the chain advanced, so the retry ceiling bounds the migrations

#### Scenario: A late result from the reclaimed original does not overwrite the clone

- **WHEN** the original body finishes after its record was reclaimed and requeued
- **THEN** the original record keeps the outcome that closed it
- **AND** the clone's outcome is unaffected

### Requirement: An instance that leaves releases what it holds

An instance shutting down SHALL release the runs it holds rather than closing them, and SHALL
address only records naming its own runtime. Records held by another instance SHALL NOT be written
by it under any shutdown path: an instance leaving knows nothing about another instance's work, and
a whole-table close turns an ordinary deploy into an outage for every owner.

Queued records SHALL NOT be touched. They belong to the queue rather than to any instance, and the
instance leaving is not the only one that could run them.

A released run SHALL be requeued through the same path a reclaimed one takes, so that a deliberate
departure and a lost instance converge on one outcome for the owner: the task runs somewhere else.

The release SHALL be a conditional write naming the instance and the status it expects, so a record
reclaimed by someone else between the read and the write is not resurrected by the instance that
used to hold it.

#### Scenario: A departing instance leaves another instance's runs alone

- **WHEN** one instance shuts down while another holds running records
- **THEN** only the departing instance's records change
- **AND** the other instance's runs continue

#### Scenario: A departing instance leaves the queue intact

- **WHEN** an instance shuts down with queued records waiting
- **THEN** no queued record changes status
- **AND** they are claimable by any instance afterwards

#### Scenario: A released run is retried, not failed

- **WHEN** an instance releases a run it holds
- **THEN** the record is requeued as a reclaimed one is
- **AND** the owner is not told the task failed

#### Scenario: A release does not resurrect a reclaimed record

- **WHEN** a record is reclaimed by another instance between the departing instance reading it and
  releasing it
- **THEN** the release applies nothing

### Requirement: Termination is a signalled, bounded drain

The process SHALL treat a termination signal as the start of a drain rather than as an immediate
exit, and the drain SHALL run in one order: stop claiming, ask every held run to stop, wait a
bounded time for bodies to return, release whatever is still held, exit. Claiming SHALL stop first,
so the drain is finite.

The drain SHALL have its own deadline, distinct from the grace period that governs a body which
never returns. That grace is sized above a tool deadline; a drain sized the same way would outlast
any orchestrator's termination window and be killed part-way through, which is the failure this
requirement exists to prevent.

Whatever the drain cannot finish SHALL be released anyway rather than waited out. The record is the
truth: a body still running against a released record finds its record no longer its own, exactly
as one whose record was reclaimed does.

The drain SHALL run before the outbound side is torn down, so that results produced while draining
can still reach their owner.

#### Scenario: A termination signal drains rather than exits

- **WHEN** the process receives a termination signal while a task is running
- **THEN** no further task is claimed
- **AND** the running task is asked to stop before the process exits

#### Scenario: A drain that overruns releases anyway

- **WHEN** a body does not return within the drain deadline
- **THEN** its record is released and the process exits
- **AND** the record is claimable by another instance

#### Scenario: A draining instance can still deliver

- **WHEN** a body returns an answer during the drain
- **THEN** the outbound path is still able to deliver it

### Requirement: A durable stop has a best-effort low-latency notification

After a stop write commits, the deciding component SHALL publish a versioned notice naming the
stored `runtime_id` and task ids whose running records were changed. Publishing SHALL happen through
a transport-independent interface and SHALL NOT occur before the durable write succeeds.

Notification loss, duplication, delay, or broker failure SHALL NOT change correctness. The existing
bounded store poll SHALL remain sufficient on its own, and notification failure SHALL NOT make the
owner's stop command fail after its durable write committed.

The consumer SHALL start before its runtime can claim work. A successful start SHALL establish its
transport cursor. A controlled broker outage MAY leave the fast path unavailable while the runtime
continues under database polling only if the consumer has first preserved a replay-safe position;
later recovery SHALL NOT anchor past notices published after that start attempt. This lifecycle SHALL
remain transport-neutral: Redis may anchor a stream position and Kafka may configure its retained
offset policy without changing the runtime or command. An unexpected startup error SHALL propagate
and prevent claims rather than silently weakening this contract.

#### Scenario: A healthy notification shortens the stop delay

- **WHEN** `/stop` records intent for a task held by a live runtime
- **THEN** that runtime's process-local signal is set without waiting for the next store poll

#### Scenario: A lost notification loses no stop

- **WHEN** the publisher or consumer loses a committed stop notice
- **THEN** the run observes the durable intent through the existing bounded store poll

#### Scenario: HTTP job stop reaches the addressed runtimes

- **WHEN** an authorized `POST /api/jobs/{job_id}/stop` commits stop intent for a running task tree
- **THEN** the browser transport publishes the same addressed notices as the slash command
- **AND** publication runs off the HTTP event loop in both single-process and multi-user deployments
- **AND** unknown or foreign jobs produce no notice

#### Scenario: HTTP notification publication fails after commit

- **WHEN** publication fails after an HTTP job stop has committed
- **THEN** the endpoint preserves its successful response and durable stop intent
- **AND** it still attempts notices for the other affected runtimes
- **AND** the existing bounded database poll remains sufficient to stop the run

#### Scenario: A notice cannot stop another runtime's task

- **WHEN** a runtime receives a notice addressed to another `runtime_id` or an unheld task id
- **THEN** none of its local cancellation signals change

#### Scenario: A notice published before the first poll is retained

- **WHEN** a consumer has completed startup and a notice is published before its first blocking poll
- **THEN** that poll returns the notice
- **AND** the runtime does not rely on the database fallback for the normal startup path

#### Scenario: The broker is unavailable during consumer startup

- **WHEN** controlled transport unavailability prevents the consumer from establishing the current cursor
- **THEN** startup records a replay-safe retained position before the runtime may claim
- **AND** a later successful poll can still read notices published after the failed attempt

