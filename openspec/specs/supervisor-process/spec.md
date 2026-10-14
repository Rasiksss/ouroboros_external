# supervisor-process Specification

## Purpose

The supervisor tier deployed as its own process: a third composition root under
`multiuser_config/supervisor/` that composes the queue, the orchestrator, the outbound bridge and the
agent-event consumer, serves the health surface only, and starts no agent runtime and no owner-facing
surface. It covers what such a process declares about itself, what it composes before it ticks, how
its drains are bounded, which refusals it makes and in what order, what its probes answer, how it is
replaced, and what several instances of it cost.
## Requirements
### Requirement: The supervisor tier has an entrypoint of its own

The repository SHALL provide an entrypoint whose composition is supervisor-only, beside the
full-runtime one and the transport one, so that the supervisor tier can be deployed as its own process
and run in several instances. It SHALL live in `multiuser_config/supervisor/` and be reachable as its
own console script.

The entrypoint SHALL name the tier it serves in its startup log, together with the message-gateway
backend and the agent-event transport it composed. A tier that is not declared is inferred, and the
inference available here — "does this environment hold model credentials" — is true of every tier in
this deployment and therefore distinguishes none of them.

Exactly one module SHALL be the substitution point for the collaborators that stop being in-process
when the tier is deployed: the message gateway, the agent-event consumer and the work signal. Swapping
a backend SHALL change that module's body and nothing else in the package, so that the process shape
and the backend choice cannot drift into one another.

The agent-event collaborator SHALL be typed as the consuming half of the reporting seam and never as
the publishing half. The supervisor reads what runs report and publishes nothing onto that seam, and a
substitution point typed loosely enough to accept a publisher is how a later swap hands this tier a
producer role nobody decided to give it.

#### Scenario: The supervisor tier is reachable as its own process

- **WHEN** the supervisor console script is started
- **THEN** the supervisor loop runs in that process
- **AND** no other tier's loop runs in it

#### Scenario: The process names what it composed

- **WHEN** the supervisor entrypoint starts
- **THEN** it logs the tier it serves, the message-gateway backend and the agent-event transport it
  composed

#### Scenario: One module is the substitution point

- **WHEN** the package is inspected for where an in-process collaborator is constructed
- **THEN** exactly one module constructs the gateway, the agent-event consumer and the work signal
- **AND** the entrypoint and the composition root name that module rather than the backends

### Requirement: The supervisor process composes the loop's collaborators and nothing that runs a task

The supervisor entrypoint SHALL compose what the loop needs to tick: the task queue, the orchestrator,
the runtime state repository, the outbound bridge over the composed gateway's outbox, the agent-event
consumer, the inbound drain, the periodic sweeps the tick performs, and the attachment-cleanup worker
the loop starts beside itself.

It SHALL NOT compose an agent runtime, a tool catalog or any tool provider, an MCP client manager, an
A2A egress manager, a distribution-extension load, a skill bridge, a browser WebSocket hub, a dialogs
transport, or a concurrency budget. None of those has a reader in a process that runs no task, and one
composed here is a resource this tier holds, fails on, and reports about on behalf of work it is not
doing.

No module in the entrypoint's package SHALL name the agent runtime, the agent package, the tool
substrate, the MCP client surface or the A2A egress manager, statically or lazily. The boundary is
already all but true of the supervisor tree itself — it names nothing under the agent packages
except the reporting seam's interfaces, and nothing from the tool substrate except the one outbound
interface its chat bridge implements — and a root that reached across it would be the first reverse
import.

The outbound bridge SHALL be constructed by this root over the outbox of the gateway it composed,
rather than resolved from a process-local registry, because it is the one place allowed to name both
halves of that seam.

It SHALL install the process-local publisher of the memory event this tier produces, before it
composes anything that could hold onto that holder. The chat writer on this tier's own outbound path
announces every persisted row, so this process is a producer of the consolidation trigger; a root
that installed none would run a deployment in which those triggers are dropped silently. Which
transport is installed SHALL follow the one broker selector rather than a decision of this root's.

#### Scenario: The tier starts no run

- **WHEN** the supervisor entrypoint has finished starting
- **THEN** no agent runtime is running in the process
- **AND** no task is claimed by this process

#### Scenario: The forbidden imports are absent

- **WHEN** the import graph of the entrypoint's package is parsed
- **THEN** no module names the agent runtime, the agent package, the tool substrate, the MCP client
  surface or the A2A egress manager

#### Scenario: The sweeps and the drain are composed

- **WHEN** the supervisor process is ticking
- **THEN** it drains the inbound seam, dispatches agent events, enforces task timeouts, reclaims stale
  leases and runs the retention sweeps
- **AND** the attachment-cleanup worker is running beside the loop

#### Scenario: The bridge names the composed outbox

- **WHEN** the outbound bridge is constructed
- **THEN** it is built over the outbox of the gateway this root composed
- **AND** it is not resolved from a process-local runtime registry

#### Scenario: The producer of the consolidation trigger has a publisher

- **WHEN** the supervisor process has finished composing
- **THEN** the process-local memory-event publisher is the one the broker selector names
- **AND** it is not the null publisher that drops silently

### Requirement: Every drain the tick performs is bounded in messages and in time

The tick SHALL bound every drain it performs, the drain of what runs report included, and that
drain SHALL be bounded **both** by how many messages it handles and by how long it spends. The two
bounds answer different failures and neither implies the other. Once this tier is deployed one
reporting seam carries the messages of every agent-runtime instance, so "drain until the seam is
empty" is a condition busy producers keep false — that is what a message count stops. A handler on
that seam may itself call a model on the tick's own thread under its own deadline, so a count of
such handlers is a tick lasting that count times the deadline — that is what a time budget stops. A
tick that cannot finish the drain enforces no timeout, reclaims no lease and drains no inbound
message, while the process still answers ready.

Either bound SHALL be a ceiling rather than a batch size: a drain that finds nothing left SHALL
stop there, and what a bound leaves behind SHALL be handled by the next tick rather than waited
for. A tick whose drain stopped at a bound SHALL NOT then wait out the inbound seam's polling
timeout, because that timeout exists to pace an *idle* loop and this one has work waiting — with
the wait, the drain's bound would double as a throughput ceiling of one drain per poll.

A drain that stopped at a bound SHALL be reported, rate-limited, because it is the only signal that
reports are arriving faster than they are handled — the state in which the seam begins discarding
what may be discarded and blocking the runs that hold the rest.

#### Scenario: A fleet reporting faster than one tick drains does not starve it

- **WHEN** runs report faster than a single tick can drain them
- **THEN** the drain stops at its message bound
- **AND** that same tick still enforces task timeouts, reclaims stale leases and drains the inbound
  seam

#### Scenario: A handler that calls a model does not extend the tick by its own deadline

- **WHEN** the reports being drained are handled by work that blocks on an external call
- **THEN** the drain ends on its time budget rather than after its message bound
- **AND** the tick reaches its sweeps and its inbound drain

#### Scenario: A drain that empties stops at the empty poll

- **WHEN** the drain finds no further report
- **THEN** it stops without waiting for either bound

#### Scenario: Saturation is visible and stated once

- **WHEN** successive ticks each stop their drain at a bound
- **THEN** the condition is reported, and repeated no more often than its own rate limit
- **AND** the tick that was saturated does not pause on the inbound seam's polling timeout

### Requirement: The supervisor process makes every refusal the mode declares, in the order that makes each honest

The supervisor entrypoint SHALL run the shared component start-up before it composes anything of its
own, and that start-up SHALL make the whole declared refusal set rather than a subset. A refusal only
one entrypoint performs is not a refusal.

The order SHALL be: the production storage guard, then the mode's configuration refusals, then the
broker initialization, then the runtime metadata store, then the schema assertion, then the deployment
keyspace comparison. The broker initialization resolves the configured broker backend, refuses the
in-process backend under the mode, and, for a backend that needs it, describes the broker connection.
Each step is what makes the next one honest. The refusals and
the broker initialization come before the keyspace comparison because that comparison *writes* the
deployment's record when none exists: a configuration the mode refuses, or whose broker the
initialization refuses, must never be the one that defines the fleet's keyspace. The store comes before the schema assertion
because the assertion reads it. The schema comes before the keyspace comparison because that
comparison reads a row a peer instance wrote, so a start against an older schema has to fail as a
schema failure rather than as a missing column.

The broker initialization SHALL be part of the shared component start-up rather than of one component,
so that every component process performing that start-up makes it before it composes a broker seam.
The refusals this step makes SHALL NOT first surface when a seam is composed, because composition runs
after the keyspace comparison. Those refusals are the in-process backend under the mode, and the
connection settings of a broker whose connection the step describes.

The keyspace comparison SHALL be part of the shared component start-up rather than of one component,
so that every component process performing that start-up makes it.

The process SHALL assert that the schema is at or above the revision this build needs and SHALL NOT
bring it up. Bringing it up is a deployment step; several supervisor instances starting together would
otherwise race one another through the migrations. The assertion is read-only, so any number of
instances MAY perform it concurrently.

This root SHALL add no refusal of its own. A tier that refused more than its peers is the same defect
in the other direction: a fleet whose processes disagree about what the configuration means.

#### Scenario: A configuration the mode refuses stops this process too

- **WHEN** the supervisor entrypoint starts with the mode declared and a configuration the mode refuses
- **THEN** startup fails with the same refusal the full runtime produces
- **AND** no route is served and no loop is started

#### Scenario: A refused configuration records no keyspace

- **WHEN** the supervisor entrypoint starts with a configuration the mode refuses and no keyspace is
  recorded for the deployment
- **THEN** startup fails on the refusal
- **AND** no deployment keyspace is recorded by this process

#### Scenario: A refused broker records no keyspace

- **WHEN** the supervisor entrypoint starts under the mode with the in-process broker backend, or with the
  Kafka broker whose connection settings are refused, and no keyspace is recorded
  for the deployment
- **THEN** startup fails on the broker refusal before the runtime metadata store is opened
- **AND** no deployment keyspace is recorded by this process
- **AND** no route is served and no loop is started

#### Scenario: The broker step runs between the refusals and the store

- **WHEN** the steps of the shared component start-up are observed in the order they run
- **THEN** the broker initialization runs once, after the production storage guard and the mode's
  refusals
- **AND** it runs before the runtime metadata store, the schema assertion and the keyspace comparison

#### Scenario: Every component start-up compares the keyspace

- **WHEN** any component process performing the shared start-up starts under the mode against a
  deployment whose recorded keyspace differs from its own
- **THEN** startup fails naming both values

#### Scenario: A schema below the build's is refused

- **WHEN** the supervisor process starts against a database below the revision the build needs
- **THEN** startup fails, naming the stored revision, the required one, and the command that brings
  the schema up
- **AND** no keyspace comparison is attempted

#### Scenario: The root carries no check of its own

- **WHEN** the entrypoint's startup path is inspected
- **THEN** it declares no configuration refusal that the shared refusal set does not declare

### Requirement: The supervisor process serves the health surface and answers about itself

The supervisor process SHALL serve the default health surface — and only that surface — so that a
deployment has a probe for the tier. It SHALL NOT mount the browser surface.

Process liveness alone SHALL NOT be the tier's readiness signal, because this tier has a failure mode
that process liveness cannot see: when the loop gives up after consecutive tick failures, the loop
returns while the process stays alive, draining no inbound and enforcing no timeout.

Readiness SHALL therefore report the loop this process hosts: not ready until the loop signals that it
has started, not ready and carrying the message once the loop reports a startup or runtime failure,
ready otherwise. The startup probe SHALL answer whether the loop has signalled that it started, at
its own path, and SHALL NOT repeat a failure the loop reported: an orchestrator asks it with the
patience a start deserves and stops asking once it has passed, and a loop that failed is replaced
through liveness within its threshold rather than waited out through the startup budget.

Readiness SHALL also report the runtime database this process composed, because a tick that cannot
read the queue is this tier doing nothing at all, and the loop answers such a tick with a backoff
rather than with a failure. The check SHALL make a bounded connection of its own rather than borrow
the pool the ticks work through — how full that pool is answers about load, not about the database,
and the instance to remove from the address is not the busiest one — and liveness SHALL NOT perform
it: a loop retrying against an unreachable database is a loop that will resume by itself, and
replacing the process would cost the runs it is waiting on.

Liveness SHALL answer success until the loop signals that it has started — that window is the
startup probe's — and after that SHALL report the loop this process hosts: a loop that reported a
failure, or a loop whose thread is alive but has not begun an iteration within the stall bound, fails
liveness. The stall bound SHALL be derived from the tick's own stop budget rather than restated, so a
step that grows cannot silently become a stall. An orchestrator restarts on liveness and not on
readiness, so a process whose loop died has to say so on liveness even though it also leaves on its
own.

The probe SHALL carry no owner-bearing figure. Queue depth, running counts and spend are readable by
this tier, and they are properties of the shared runtime database rather than of the answering
process; publishing them on an unauthenticated probe would put one deployment's aggregate on the one
surface that is required to carry no owner's data. A deployment that wants those figures reads them
from the database every instance already shares.

The process SHALL publish on its application state exactly the values a mounted route reads, and with
this surface that is the one probes object the startup, readiness and liveness routes read. It SHALL NOT publish a drive root, a
message gateway, a conversation id, a runtime-status provider or a restart hook, none of which any
mounted route reads. It SHALL likewise not configure a process-wide readiness holder whose only
readers are components it did not compose.

This tier SHALL install no browser identity resolver and SHALL resolve no conversation id: it mounts
no route that resolves identity and stamps no row that carries a conversation.

#### Scenario: The surface is health only

- **WHEN** the routes the supervisor process mounts are listed
- **THEN** they are the startup probe, the readiness probe, the liveness probe and the core health route
- **AND** no browser route is among them

#### Scenario: Readiness follows the hosted loop

- **WHEN** the supervisor process is listening and its loop has not yet signalled that it started
- **THEN** readiness and startup answer not ready, and liveness answers success
- **AND** readiness and startup answer ready once the loop signals that it started

#### Scenario: A loop that gave up is visible on the probe

- **WHEN** the loop reports a startup or runtime failure
- **THEN** readiness answers not ready and carries the reported message
- **AND** liveness answers failure carrying the same message
- **AND** startup answers success: the start is over, and the replacement is liveness's

#### Scenario: A database the ticks cannot reach is visible on readiness

- **WHEN** the runtime database refuses the connection this process holds for the probe, while the
  loop is running
- **THEN** readiness answers not ready
- **AND** liveness answers success, because the loop resumes when the database returns

#### Scenario: A loop that stops ticking fails liveness and nothing else

- **WHEN** the loop's thread is alive but no iteration has begun within the stall bound
- **THEN** liveness answers failure naming how long the loop has been silent
- **AND** readiness is unaffected, and a further iteration restores liveness

#### Scenario: The probe carries no owner-bearing figure

- **WHEN** the health surface is requested without any identity headers
- **THEN** the answer names no owner, no queue depth, no running count and no spend

#### Scenario: Application state carries only what a route reads

- **WHEN** the keys the supervisor application publishes on its state are compared with the keys the
  mounted routes read
- **THEN** the two sets are the same

### Requirement: A supervisor process is replaced by its orchestrator, not by itself

The supervisor process SHALL bind the host and port it was configured with, through the same settings
the other entrypoints read, and SHALL exit with a failure when that address is unavailable. It SHALL
NOT scan for a free port, SHALL NOT drift to a neighbouring one, SHALL NOT publish the port it settled
on to a shared file, and SHALL NOT terminate whatever process holds a port it wanted.

The process SHALL expose no hook that restarts or re-execs itself. The restart hook the loop requires
SHALL refuse, naming the tier, so that an owner's restart command is answered and does not proceed. A
restart handled here would restart a process that runs no task:
the owner's runs are held by agent-runtime instances this process cannot re-exec, so the restart would
report success and change nothing. The refusal SHALL be the supplied hook rather than a new check on
the command path, so that no second enforcement point is created.

Refusing before the restart proceeds SHALL also mean that no restart marker is written to this
process's local disk, which several instances would each write and none would read.

When the loop stops for any reason, the process SHALL stop serving and exit, reporting a failure exit
code when the loop reported an error and success when the stop came from a termination signal. A
process that kept serving a probe after its loop had stopped would be answering for a tier it is no
longer part of. Asking the server to exit SHALL be conditioned on the loop having stopped *first*:
an ordinary shutdown stops the loop too, so a process that inferred a dead loop from its own stop
signal would report one on every deploy.

The wait a stopping process gives the loop SHALL exceed the longest single step a tick can be
inside, so that an ordinary termination does not abandon the loop in the middle of a write; it SHALL
remain bounded, and an expiry SHALL be reported as the failure it is rather than exited as a clean
stop.

A termination signal SHALL stop the loop and end the process, leaving the decision to start a
replacement to whatever placed it.

#### Scenario: A busy address fails the process

- **WHEN** the configured address is already in use
- **THEN** the process exits with a failure naming the host and port it could not bind
- **AND** it does not attempt another port, write a port file, or terminate the holder

#### Scenario: An owner's restart is answered and refused

- **WHEN** an owner sends the restart command to a supervisor process of this tier
- **THEN** the owner is told the restart was cancelled
- **AND** the process does not re-exec, and writes no restart marker to local disk

#### Scenario: A dead loop ends the process

- **WHEN** the supervisor loop returns after reporting a failure
- **THEN** the process stops serving and exits with a failure code

#### Scenario: A termination signal ends the process

- **WHEN** the supervisor process receives a termination signal
- **THEN** the loop stops, the attachment-cleanup worker stops, and the process exits successfully
- **AND** it starts no replacement of itself
- **AND** nothing reports that the loop died of its own accord

#### Scenario: A loop that will not stop is not a clean exit

- **WHEN** the loop has not stopped by the time the process's wait for it expires
- **THEN** the process leaves, and reports the abandoned loop as a failure

### Requirement: Several supervisor instances run the same sweeps, and the cost is accounted for

The periodic sweeps the tick performs address records by what the record says rather than by which instance is running, so every instance sweeps every record in the deployment. That SHALL be true by design — a sweep that only its own instance's records could reach would leave a dead instance's work unreclaimed — and the duplication it produces SHALL be accounted for rather than discovered.

Retention cleanup SHALL acquire a nonblocking deployment-wide database claim before checking its interval or deleting records. A competing instance SHALL skip that pass. The claim SHALL be released when the pass ends or its database transaction closes.

A due retention sweep SHALL continue in bounded batches until an empty batch confirms completion. It SHALL yield to the next supervisor tick once its pass budget is exhausted, without advancing the interval checkpoint. Partial deletion and failures SHALL leave cleanup due. Only completion SHALL start the next interval. Scratchpad partition limits SHALL NOT be mistaken for sweep completion.

Attachment cleanup duplication remains accepted idempotent cost: it removes what is already removed.

A sweep whose writes are conditional SHALL remain conditional, so that exactly one instance acts and the others take no further step: a lease reclaim's write decides in the statement, and an instance that loses it SHALL send no notice and requeue nothing.

A sweep that duplicates *visibly* — a message the owner receives more than once, or work enqueued more than once — SHALL NOT be left to the deployment to discover. Two exist on the timeout sweep: the soft-timeout notice, whose flag is read from a listing and written last-writer-wins, and the hard-timeout kill, whose terminal guard admits a repeat because a write of the status a record already holds still applies — producing both a second notice and a second retry clone under a fresh id. Both are owned by a change of their own and SHALL be fixed there, before a deployment runs more than one instance of this tier.

#### Scenario: A stale lease is reclaimed once

- **WHEN** several supervisor instances sweep the same stale lease
- **THEN** exactly one reclaims it, notifies the owner and requeues the work
- **AND** the others take no further step on that record

#### Scenario: Retention backlog exceeds one batch

- **WHEN** expired records exceed one bounded deletion batch
- **THEN** cleanup continues in further batches, yielding to later ticks when its budget is spent
- **AND** the interval checkpoint advances only after an empty batch

#### Scenario: Several replicas attempt retention cleanup

- **WHEN** one instance holds the retention claim
- **THEN** a competing instance skips the pass without waiting for the winner
- **AND** the claim becomes available after the winner finishes or its transaction closes

#### Scenario: Cleanup fails after deleting a batch

- **WHEN** a subsequent batch or checkpoint save fails
- **THEN** committed deletions remain and the claim is released
- **AND** the interval checkpoint is not advanced, allowing a later tick to resume

#### Scenario: Attachment duplication is stated

- **WHEN** attachment cleanup runs on several instances
- **THEN** repeated deletion remains accepted idempotent cost

#### Scenario: Visible duplication is owned before the tier is scaled

- **WHEN** the timeout sweep's duplicate soft notice and duplicate hard-timeout kill are considered
- **THEN** they are named as belonging to a change of their own
- **AND** more than one instance of this tier is not deployed until that change has landed
