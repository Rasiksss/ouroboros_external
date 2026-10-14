# memory-distillation Specification

## Purpose

How distillation is executed: by the background memory runtime loop running in a thread of its own,
started by the composition root under the same provider gate and stop signal as the supervisor
thread. The loop and thread are shared with the other background memory handlers; distillation
never requests a follow-up, and a handler failure never ends the loop.
## Requirements
### Requirement: The agent publishes a raw event and decides nothing

On finishing a task that did not fail, the agent SHALL publish one distillation event describing the
completed task and its response, naming the scope the task ran under. That scope SHALL be the one the
run already holds, passed through rather than minted: the publisher is inside a run whose identity is
settled, and a second mint would answer the same question differently. The agent SHALL NOT evaluate
any eligibility rule, read the memory feature flag, truncate any field, count pending work, or write
a task row. A task that failed SHALL produce no event.

#### Scenario: A completed task is reported

- **WHEN** a task finishes without failure
- **THEN** exactly one distillation event is published, carrying the scope the task ran under, the
  task's id, type, text, and completion timestamp, plus the response text and the execution trace

#### Scenario: The event names no identity beside the scope

- **WHEN** the published event and its task reference are inspected
- **THEN** neither carries a chat id, an owner id, or any other identity field of its own

#### Scenario: A failed task is not reported

- **WHEN** a task finishes as failed
- **THEN** no distillation event is published

#### Scenario: The agent no longer reaches the durable queue

- **WHEN** the agent package's imports are inspected
- **THEN** no module imports the supervisor's task queue

### Requirement: Eligibility is decided by the handler

The handler SHALL decide whether a consumed event is worth distilling, before doing any work on it.
That decision SHALL live outside the transport and outside the agent, and SHALL be private to the
handler: the loop that consumes events SHALL apply no rule of its own and SHALL pass every event of
a known type straight through. An event SHALL be skipped when the memory distillation setting is
disabled, when the task text is empty, when the response text is shorter than 80 characters, when
the task type is one of `cron`, `system`, `health_check`, or `heartbeat`, or when the execution
trace carries evidence that is unsafe to turn into durable facts. A skip SHALL log a warning naming
the reason and the source task id, and SHALL run no model call.

#### Scenario: A short response is skipped

- **WHEN** an event whose response text is shorter than 80 characters is consumed
- **THEN** no distillation runs
- **AND** a warning names the reason and the source task id

#### Scenario: An unsafe trace blocks distillation

- **WHEN** the trace contains a tool failure that no later successful call with the same tool and
  argument fingerprint recovers, a failure without such a fingerprint, or a data-provenance warning
- **THEN** no distillation runs
- **AND** a warning names the blocking evidence and the source task id

#### Scenario: An eligible event becomes a request

- **WHEN** an event passes every rule with the setting enabled
- **THEN** the handler bounds it into a request and runs it

#### Scenario: The loop passes every known event through

- **WHEN** the consuming loop reads an event of a type it has a handler for
- **THEN** it hands the event to that handler unchanged, applying no rule of its own

### Requirement: The request carries a bounded, self-contained payload

A distillation request SHALL carry the task text truncated to 800 characters, the response text
truncated to 1200 characters, a trace summary truncated to 1200 characters, the scope the source task
ran under, the source task id, and the completion timestamp. The handler SHALL need no further source
of task data to run, and SHALL NOT mint a scope: the whole point of a self-contained request is that
the identity it is executed under is the identity that produced it.

#### Scenario: Oversized fields are truncated with a notice

- **WHEN** a request is built from an event whose fields exceed their caps
- **THEN** each field is truncated to its cap and marked as truncated

#### Scenario: The request names the scope the task ran under

- **WHEN** a request is built from an eligible event
- **THEN** it carries that event's scope unchanged

#### Scenario: The handler mints no scope

- **WHEN** the distillation handler's module is inspected
- **THEN** it constructs no scope and imports no scope mint

### Requirement: The memory timestamp travels in the event

The timestamp used to stamp saved memories SHALL be taken from the event, produced by the publisher
at the moment the task completed. The handler SHALL NOT read the task store to resolve it, because
it now runs before the source task is recorded as finished, where the store would yield the task's
creation time instead.

#### Scenario: Saved memories carry the completion time

- **WHEN** the handler saves a memory for a request
- **THEN** the memory is stamped with the timestamp carried by the request

#### Scenario: The handler does not read the task store for the stamp

- **WHEN** the handler runs with a request
- **THEN** it performs no task-store lookup to resolve the stamp

### Requirement: Handler execution preserves current distillation behavior

The handler SHALL run the existing memory tool loop against the request: at most 4 rounds over the
`memory_save` and `memory_list` tools, no new round started after a 45-second deadline, sticky
routing once a round succeeds on the fallback model, a deduplication manifest capped at 60 entries
and 4000 characters, the kind enum narrowed to what an automated caller may write, and
`NOTHING_TO_SAVE` as the outcome when there is nothing durable. Every model call's token figures
SHALL accumulate into the run's reported totals.

The deduplication manifest and the tool context the run executes under SHALL both address the
request's scope, so the list the model is shown to deduplicate against comes from exactly the
namespace the saves will land in.

The loop SHALL be asynchronous: each round's model call and each tool call SHALL be awaited, and the
handler SHALL NOT block the event loop it runs on at any point. The deadline SHALL be measured over
that asynchronous execution and SHALL keep its current meaning — it stops a new round from starting
and does not interrupt one in progress.

#### Scenario: Nothing durable produces no memory

- **WHEN** the model answers that there is nothing to save
- **THEN** the run completes with that outcome and no memory is written

#### Scenario: The deadline stops new rounds

- **WHEN** the deadline passes after at least one completed round
- **THEN** no further round starts and the run completes with what it has

#### Scenario: The dedupe list and the saves share one namespace

- **WHEN** a run builds its manifest and then saves a memory
- **THEN** both address the request's scope
- **AND** a memory already stored under that scope appears in the manifest the model is shown

#### Scenario: A run in progress does not stall its neighbours

- **WHEN** a distillation run is inside a model call or a tool call and another delivery is scheduled
  on the same event loop
- **THEN** that other delivery runs

#### Scenario: The bounds are the ones that were there before

- **WHEN** a run's rounds, deadline, manifest caps and kind narrowing are compared with the
  synchronous implementation's
- **THEN** each is the same value and has the same effect

### Requirement: Distillation reports only through its observability event

Distillation SHALL NOT create, claim, or close a task row. It SHALL emit no task-completion event
and SHALL NOT participate in queue status, task listings, restart durability, or timeout enforcement.
It SHALL emit no model-usage event either: there is no task row to attribute a call to, and per-call
usage accounting has been retired. The handler SHALL therefore need no outlet onto the supervisor's
event stream at all. The outcome of every run — completed with saved names, nothing to save, or
failed — SHALL be recorded as an observability event carrying what the run consumed and the scope it
ran under, so a run leaves a durable trace without a task row. Unlike consolidation, that trace
SHALL name all four axes: a distillation run really is one task's run, in one chat, on one surface.

#### Scenario: A run emits no event onto the supervisor's stream

- **WHEN** a distillation run finishes, whether it saved anything or failed
- **THEN** no task-completion event and no usage event is emitted, and no task row is written

#### Scenario: The outcome is observable

- **WHEN** a distillation run finishes
- **THEN** an observability event records the status, the scope, the saved memory names, the round
  count, the token totals, and the source task id
- **AND** a round whose response was rejected still counts toward those totals

### Requirement: The retired task type is never executed

The system SHALL treat `memory_distillation` as a retired task type. Rows of that type left in a
runtime database by an earlier version SHALL be excluded from every worker's claim and SHALL be
swept to a terminal status when the executor starts. No such row SHALL ever be handed to the agent
loop.

#### Scenario: A leftover row is retired at startup

- **WHEN** the executor starts with rows of the retired type in a non-terminal status
- **THEN** each is moved to a terminal status naming the retired type as the reason

#### Scenario: A leftover row is never claimed

- **WHEN** a row of the retired type is present and a worker looks for work
- **THEN** the row is not claimed by any worker, including the foreground worker

### Requirement: Distillation runs on the shared memory loop, which owns no thread

Distillation SHALL be executed by the background-memory runtime loop, which SHALL run as a task owned
by the composition root rather than in a thread of its own. It SHALL be started under the same
provider gate and stop signal as the rest of the process's background work, and SHALL be awaited on
the way out rather than abandoned.

The loop SHALL be shared with the other background memory handlers rather than belonging to
distillation alone. It SHALL poll the consumer and start a task for the handler registered for the
event's type, and SHALL log a run's human-readable result from the handler that produced it rather
than from the loop, so the loop names no handler's result type.

Distillation SHALL never request a follow-up delivery, so one distillation event occupies one
delivery exactly once. A handler failure SHALL be logged with its traceback, SHALL fail only its own
delivery, and SHALL NOT end the loop or disturb another delivery. Setting the stop signal SHALL end
the loop after the deliveries in flight have drained.

No module of the background memory package SHALL create a thread for this.

#### Scenario: A handler failure does not kill the loop

- **WHEN** the handler raises while processing an event
- **THEN** the failure is logged with its traceback
- **AND** the loop processes further events, and other deliveries in flight are unaffected

#### Scenario: Shutdown ends the loop

- **WHEN** the stop signal is set
- **THEN** the loop starts no further delivery
- **AND** it returns once the deliveries in flight have drained or their bound has passed

#### Scenario: The loop starts with the application

- **WHEN** the application starts with a usable model provider configured
- **THEN** the publisher is installed and the memory runtime loop is running as a task
- **AND** starting it twice does not create a second loop

#### Scenario: The loop owns no thread

- **WHEN** the package's modules are inspected while the loop is running
- **THEN** no thread was created for the loop or for any delivery it started

#### Scenario: A distillation run asks for no follow-up

- **WHEN** the distillation handler finishes an event, whether it distilled, skipped, or failed
- **THEN** it requests no re-delivery
- **AND** the completed or failed result line is emitted by the handler

### Requirement: A memory tool body runs off the event loop on an executor the call owns

A tool body the handler invokes SHALL NOT run on the event loop, and SHALL NOT be placed off it
through the shared process-wide worker pool. Each call SHALL own the executor it runs its body on,
and that executor SHALL be discarded with the call.

The reason is the one already recorded where tool placement is owned: a single process-wide pool
turns a hung body into a slot held for as long as the body runs, so N hangs starve every later call
in the process. A per-call executor localises that cost to the call that hung.

The body SHALL run with a copy of the calling context, so that the operation identifier, the scope
and anything else the handler established reaches it. A body run with an empty context makes
uncorrelated outbound calls and reports them under no operation.

A body that outlives its deadline SHALL be abandoned rather than awaited, and abandoning it SHALL NOT
hold anything the next call needs.

The tool bodies themselves SHALL NOT be rewritten for this. What changes is where they run, not what
they do.

#### Scenario: A tool body does not run on the loop

- **WHEN** a memory tool body is invoked during a distillation run
- **THEN** it runs off the event loop
- **AND** other work scheduled on that loop runs while it does

#### Scenario: The shared pool is not used for a tool body

- **WHEN** the handler's module is inspected
- **THEN** it does not place a tool body on the shared process-wide worker pool

#### Scenario: A hung body does not starve later calls

- **WHEN** a tool body hangs past its deadline and further tool calls are made afterwards
- **THEN** those calls proceed

#### Scenario: The body sees the calling context

- **WHEN** a tool body runs during a delivery with an operation active
- **THEN** it observes that operation
- **AND** an outbound request it makes carries the operation's trace header

#### Scenario: The tool bodies are unchanged

- **WHEN** the memory tool implementations are compared before and after
- **THEN** their behaviour and their signatures are the same

