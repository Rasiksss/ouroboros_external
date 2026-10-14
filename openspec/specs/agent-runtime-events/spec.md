# agent-runtime-events Specification

## Purpose
The seam between a task run and whoever consumes its reports — progress, completion, tool calls, typing signals.
## Requirements

### Requirement: Publishing and consuming are separate interfaces

What a run reports outward SHALL cross two abstract interfaces: a publisher exposing
`publish(event)` and a consumer exposing `poll(timeout)`. A run SHALL depend on the publisher only,
and the component handling those reports SHALL depend on the consumer only. Neither interface SHALL
name a queue, a topic, a broker or any other transport in its signatures.

Concrete transports SHALL subclass these ABCs, and an implementation missing an abstract member
SHALL NOT be instantiable. A run holding an object it could also read from is a run that can consume
its own reports, which is the shape that makes a second instance impossible to add later.

#### Scenario: A run's dependency cannot consume

- **WHEN** the object a run holds for reporting is inspected
- **THEN** it satisfies the publisher interface
- **AND** it exposes no method for reading events back

#### Scenario: The consumer's dependency cannot publish

- **WHEN** the object the consuming component holds is inspected
- **THEN** it satisfies the consumer interface
- **AND** it exposes no method for publishing

#### Scenario: An incomplete implementation is rejected

- **WHEN** a class claims to implement either interface but omits an abstract member
- **THEN** it cannot be instantiated

### Requirement: Messages are named types under one base, and every one names its scope

Every message crossing the seam SHALL be a named frozen dataclass, not a dictionary, and all of them
SHALL derive from one base type to which the consumer's `poll` is typed, so a new kind of report
needs no interface change.

Every message SHALL carry the `OwnerScope` of the work that produced it. That field SHALL be
declared once, on the base, and SHALL be required with no default: a kind added later cannot forget
to say whose it is, and a transport can read identity off any message without learning a kind — a
partition key is the one thing a broker needs and the one thing these interfaces must not learn a
kind to obtain.

No message SHALL carry a chat id, an owner id or any other identity field beside the scope.

Every field of every message SHALL be a value a transport can serialize. A message SHALL NOT carry a
live object whose meaning depends on this process — an open handle, a callable, a store, a queue.
This is what the current `dict[str, Any]` payloads violate by carrying a live `OwnerScope` instance
inside them, and it is why the shapes are named now rather than at the moment a broker needs them.

#### Scenario: A message cannot be constructed without a scope

- **WHEN** any message kind is constructed with no scope
- **THEN** construction raises

#### Scenario: Identity is declared once, on the base

- **WHEN** the message types are inspected
- **THEN** the scope is declared on the base type
- **AND** no kind declares a chat id, an owner id or any other identity field of its own

#### Scenario: A new report kind needs no interface change

- **WHEN** a new message type is added as a subclass of the base
- **THEN** neither the publisher interface nor the consumer interface changes to carry it

#### Scenario: Every message is serializable

- **WHEN** each message kind is inspected
- **THEN** every field is a value type, and none holds a callable, a connection, a queue or a store

### Requirement: A report never carries a fact its own run must record

A message on this seam SHALL be a report about work, never the act of recording it. A fact the run
itself observes and the store must hold — that the task reached a terminal status, that the run is
alive — SHALL be written by the run, directly, and MAY additionally be reported for display and
observability.

The rule follows from what the seam is for. Once the consumer can be a different process from the
run, a write it performs on the run's behalf is a fact recorded by a component that did not witness
it, arriving after the witness could already have recorded it. This is the reason `task_heartbeat`
already reports a lease the body wrote, and it applies unchanged to every other fact of the same
kind.

Losing a message on this seam SHALL therefore cost a display update, an accounting row or an echo —
never the completion, the liveness or the delivery of a task.

The rule holds in the inverse direction too: a report that cannot be published SHALL NOT change the
outcome of the run it describes. A publisher MAY raise when the transport is unreachable, and the caller
SHALL handle that where it publishes rather than letting it reach a handler that closes the task. A
transport failure that rewrote a successfully completed record as failed would be the same defect from
the other side — a component that did not witness the run deciding what the run did.

#### Scenario: The terminal status is not written by the consumer

- **WHEN** the report that a task finished is handled
- **THEN** no status transition is written by the handler
- **AND** the record is already terminal, written by the run

#### Scenario: A lost report does not lose a task

- **WHEN** every report from a completed run is discarded before reaching the consumer
- **THEN** the task's record is still terminal with its result
- **AND** the task is not reclaimed and not re-executed

#### Scenario: Liveness is still the body's to write

- **WHEN** the report that a run is alive is handled
- **THEN** the handler writes no lease
- **AND** the lease was already advanced by the running body

#### Scenario: A failed publish does not fail the run

- **WHEN** publishing a completed run's reports raises because the transport is unreachable
- **THEN** the task's record keeps the terminal status and result the run wrote
- **AND** the failure is logged where the publish happened

### Requirement: Delivery is at most once, settled when it is handed over

Every transport behind this seam SHALL deliver each accepted report at most once, apart from the one
duplicate a failed settlement admits, stated below. A consumer SHALL settle a report, or issue the
request that settles it, before or as it hands the report to its caller. Settling means recording that
the report was taken. So a report read by an instance that then dies is lost, not delivered again, and
no report kind SHALL be required to carry a deduplication id.

The rule follows from what the consuming handlers do. They show text to the owner, bill usage,
enqueue subtasks and delete staged files, and none of them can recognise a second copy. Losing a report
already costs only what the rule "A report never carries a fact its own run must record" allows.
Delivering one twice costs a repeated answer, a double charge or a second subtask, and nothing
downstream can undo that.

On some backends settlement is a separate request that can fail after the report has left the
broker's hands. Such a backend SHALL still return a report whose settlement failed, and SHALL repeat
the settlement before it takes the next report. While the repeat fails because the broker cannot be
reached, it takes nothing. When the repeat is refused because the consumer's membership has already
ended, the position the broker holds is the older one, and the consumer goes on only by rejoining.
Withholding the report would not return it to the broker: the
consumer's position has already moved past it, so the next settlement of a later report would lose it
with nothing failing loudly. Such a backend SHALL state the one duplicate this admits in the
architecture document's description of that backend. If the report's partition is reassigned before
the repeated settlement succeeds, whether to another consumer or back to the same one after it rejoins,
the partition's next owner resumes at the last settled position and delivers that report once more.
No other report is delivered twice.

A publisher SHALL NOT repeat an append that raised. On some brokers an append cannot be cancelled once
it has been handed to the client. There, a publish whose bounded wait for the broker expired MAY still
reach the consumer, and it is delivered at most once, like any other report, because nothing appends
it again. A call site that handles such a failure SHALL NOT assume the report reached nobody: a call
site that performs the consuming handler's work in its own process when the publish raises records
that work twice if the report lands later. The run's outcome stays what the run wrote.

#### Scenario: A report is settled before it is returned

- **WHEN** a consumer returns a report from `poll` while the broker answers
- **THEN** the transport already records that report as taken
- **AND** no consumer polling after that receives it

#### Scenario: A report taken by an instance that then dies is not redelivered

- **WHEN** a consumer returns a report whose settlement succeeded, and its instance dies before a
  handler finishes with it
- **THEN** no other consumer receives that report

#### Scenario: A report whose settlement failed is still returned

- **WHEN** the request that settles a report fails as the report is taken
- **THEN** `poll` still returns that report
- **AND** the settlement is repeated before the consumer takes its next report

#### Scenario: A failed settlement followed by a reassignment admits one duplicate

- **WHEN** a report's settlement failed and the report's partition is reassigned, to another consumer
  or back to the same one, before the repeated settlement succeeds
- **THEN** the partition's next owner may deliver that report once more
- **AND** no other report is delivered twice

#### Scenario: A publish that raised is not appended again

- **WHEN** a publish raises because its bounded wait for the broker expired
- **THEN** the publisher does not append that report again
- **AND** if the broker had accepted the report, the consumer receives it at most once

### Requirement: A message the user is waiting for is not dropped by its publisher

No transport behind this seam SHALL silently drop a message whose loss the user would observe. Where
the transport is bounded, publishing into a full one SHALL wait for room rather than discarding, and
a run blocked on that wait SHALL be the intended outcome: a run that cannot report its answer must
not race ahead of its own output.

A kind whose loss costs nothing observable — a typing indicator, a progress ping already superseded —
MAY be dropped, and a kind that may be dropped SHALL say so on itself rather than being decided by
which queue it happened to land in.

This inverts the rule the background-memory transport uses, and deliberately: there a dropped trigger
is regenerated by the next event, while here a dropped message is the user's answer and regenerates
nowhere.

A transport whose bound the deployment sizes, and which the broker rather than the publisher enforces,
SHALL NOT meet that bound
by discarding a message it has already accepted. A retention policy that evicts by age evicts the message
that has waited longest to be delivered, which is the one a user is waiting for and never the indicator
that supersedes itself — so the bound SHALL be applied before a message is accepted, and a message
already accepted SHALL be delivered or lost only with the transport itself.

A transport that keeps its reports in a log whose retention the infrastructure running the broker sets,
rather than in memory the deployment sizes, SHALL apply no bound at the publisher. It holds nothing in
the process while its consuming tier is stopped, and its broker keeps the backlog on disk, so it SHALL
append every report it is handed without reading the consuming group's position and without waiting.
That retention is not a bound this requirement governs: a report left unread for longer than it is lost
with the log, and that cost SHALL be stated where the backend is described. The Kafka backend is such a
transport.

A publisher that waits for room SHALL make the wait visible, naming the backlog and the bound on a
bounded interval, so a consuming tier that has stopped draining is legible from the producer's own logs
rather than only from the broker.

Where the consumer is another process, the wait SHALL be bounded. A publisher whose transport stays
full for `MAX_BACKPRESSURE_WAIT_SEC` SHALL refuse the report with `AgentEventBacklogFull`, an error of
its own type. The constant and the error are declared beside the seam's interfaces, so every such
backend refuses at the same bound with the same type. The bound exists because a publisher that could
wait forever would hold its thread for as long as a consuming tier stays stopped. Publishes run on
worker threads the process joins at exit, so an unbounded wait is a runtime that cannot stop and does
not answer a termination signal. The refusal is loud, never a silent discard. Its own type lets a call
site tell a transport nobody drains from a broker that cannot be reached. Each is logged where the
publish happened, and neither decides the outcome of the run.

#### Scenario: A full transport blocks rather than dropping an answer

- **WHEN** the transport is at capacity and a run publishes the message carrying its answer
- **THEN** the publish waits for room
- **AND** the message is delivered once room is available, within the bounded wait where the transport
  bounds it

#### Scenario: A droppable kind says so itself

- **WHEN** a kind that may be discarded under pressure is inspected
- **THEN** the kind itself declares that it is droppable

#### Scenario: The bound is applied before acceptance, not by eviction

- **WHEN** a bounded transport is at capacity
- **THEN** it refuses or delays the message being offered
- **AND** no message it has already accepted is evicted to make room

#### Scenario: A waiting publisher says what it is waiting on

- **WHEN** a publisher has been waiting for room for a bounded interval
- **THEN** it logs the backlog and the bound it is waiting on

#### Scenario: A wait that outlasts its bound is refused, not held forever

- **WHEN** a transport whose consumer is another process stays at capacity for
  `MAX_BACKPRESSURE_WAIT_SEC` while a kind that is not droppable is being published
- **THEN** the publish raises `AgentEventBacklogFull`, naming the backlog and the bound
- **AND** the report is not queued

#### Scenario: A transport whose retention the infrastructure sets appends without waiting

- **WHEN** the consuming tier of such a transport has stopped draining and a run publishes reports,
  droppable or not
- **THEN** every report is appended without waiting, and the publisher drops none
- **AND** the publisher reads no position of the consuming group

### Requirement: Ordering is guaranteed within one scope and nowhere else

Messages published for one scope SHALL be delivered to the consumer in the order they were published.
No ordering SHALL be assumed or required between messages of different scopes.

A caller that needs one thing to happen before another SHALL express it as an ordering within a
scope, and a caller that needs a store write ordered against a message SHALL perform the write itself
in the order it needs. An ordering that holds only because one implementation happens to be a single
FIFO queue is an ordering that breaks on the day the transport is partitioned, with no call site
changing.

A backend that delivers to competing consumers in several processes MAY deviate. Such a backend hands
different messages to different readers, so two messages of one scope are handled by two processes at
once and neither the transport nor the deployment can order them. A backend taking this deviation SHALL
state it where it implements it, naming what a reader loses — the order of a run's own reports, and with
it the display order of a progress line against the answer that followed it — and SHALL declare it to the
conformance suite through `preserves_order_within_one_scope`. That declaration raises rather than
defaulting, so removing it fails the suite and the next reader finds a declaration rather than an
apparent oversight. The deviation is a property of that backend, not a licence in the seam: a backend
that can order by the partition key SHALL NOT take it, and a backend that has taken it SHALL still answer
the partition-key question, because that answer is what a keyed broker later uses to remove the
deviation.

A keyed, partitioned backend orders by the partition key. It routes every report of one scope to one
partition and hands each partition to one consumer of the group at a time. Such a backend SHALL declare
that it preserves per-scope order and SHALL NOT take the multi-reader deviation. The order SHALL hold
when several consumers share the group and when a partition moves between them.

A rebalance moves a partition only once the previous owner polls again, which its caller does after it
has finished with the report in hand, or once the broker has evicted the previous owner. The next owner
then resumes after the last report that was settled. There is one stated exception, at the boundary of
a consumer the broker evicts while it is still alive: one that has not polled within its poll interval,
or whose heartbeats have not reached the broker within its session timeout. The evicted consumer may
still be handling a report while the partition's next owner hands over the report that followed it.
The backend SHALL state this exception in the architecture document's description of that backend,
together with the poll interval and the session timeout that bound it.

#### Scenario: One scope's messages arrive in order

- **WHEN** a run publishes several messages for one scope
- **THEN** the consumer receives them in publication order

#### Scenario: The scope is the ordering key

- **WHEN** the transport is asked what it orders by
- **THEN** it reads the scope off the base message type without naming any kind

#### Scenario: A multi-reader backend declares what it does not order

- **WHEN** a backend delivering to competing consumers across processes is inspected
- **THEN** the module implementing it states that per-scope delivery order is not preserved, and what
  that costs a reader
- **AND** its conformance subclass declares `preserves_order_within_one_scope` as false, so removing
  the declaration fails the suite
- **AND** the backend still answers the partition-key question with the scope

#### Scenario: A keyed backend keeps one scope's order across consumers and a rebalance

- **WHEN** a run publishes several messages for one scope to a keyed, partitioned backend whose group
  has several consumers
- **AND** the partition carrying that scope moves from one consumer to another while the messages are
  being consumed, with every settlement succeeding
- **THEN** the messages of that scope are handed over in publication order across both consumers
- **AND** none of them is handed over twice

#### Scenario: A keyed backend declares that it preserves order

- **WHEN** a keyed, partitioned backend is held to the conformance suite
- **THEN** its subclass declares `preserves_order_within_one_scope` as true
- **AND** the backend takes no multi-reader deviation, and the architecture document's description of
  it states the eviction of a live consumer as the one boundary exception

### Requirement: The transport delivers messages unchanged

A transport implementation SHALL deliver what it was given and nothing else. It SHALL NOT apply
eligibility rules, truncate fields, convert a message into another type, read settings or read the
store. A message published SHALL be returned by a poll as the same value, with the same nested parts.

#### Scenario: A published message round-trips intact

- **WHEN** a message is published and then polled from the same transport
- **THEN** the polled value equals the published one, including its nested parts

#### Scenario: The transport does not reach the business layer

- **WHEN** the transport module's imports are inspected
- **THEN** it names neither the agent nor the handlers that consume its messages

### Requirement: Polling names a timeout

The consumer interface SHALL accept a timeout on `poll`, defaulting to non-blocking. A caller SHALL
NOT need its own sleep to avoid spinning, and a poll that finds nothing within the timeout SHALL
return nothing rather than raising.

A zero timeout SHALL still return the next report the broker already holds for a consumer that holds
the report's partition, or the whole transport where it has no partitions. The consuming component
drains with zero-timeout polls on every pass. A poll that returned only what a client had already
buffered would therefore deliver every first report after an idle period one whole pass late. On a
backend whose client fetches in the background, a zero-timeout poll SHALL make one fetch, bounded by a
constant of that backend small enough that an empty poll still returns immediately, and return what
that fetch received. On a broker that answers, the poll SHALL NOT wait for a report beyond that bound;
the round trip that settles the report it returns, or that checks an idle broker, comes on top of it.

#### Scenario: An empty transport returns nothing immediately

- **WHEN** `poll` is called with no timeout on an empty transport
- **THEN** it returns nothing without blocking and without raising

#### Scenario: A waiting poll returns nothing after its timeout

- **WHEN** `poll` is called with a timeout on an empty transport
- **THEN** it returns nothing after approximately that timeout

#### Scenario: A zero-timeout poll returns a report already published

- **WHEN** a consumer that already holds a partition polls with no timeout, while the broker answers,
  after a report for a scope on that partition was published and nothing published before it on that
  partition is still unread
- **THEN** the poll returns that report
- **AND** it returns within the backend's zero-timeout fetch bound plus the round trip that settles
  the report

### Requirement: A report round-trips as its declared types

A transport that serializes SHALL return each message as the kind it was published as, with every field
as its declared type, not as an equal collection of JSON values. Decoding SHALL reconstruct through the
constructors, so `OwnerScope` validation re-runs on the receiving side — the one hop where nobody else is
watching.

The wire form SHALL name the kind, because the consuming side dispatches on the message type itself and
has no string to fall back on. A field that decodes to a value which is not an instance of its declared
type SHALL fail to decode rather than being delivered: a wire `"false"` where `is_progress` is declared
`bool` turns every answer into a progress tick, and a string where a mapping is declared iterates as
characters — both are wrong objects rather than failures.

A key the decoding build does not know SHALL be ignored and the message still delivered, because
instances are upgraded one at a time; a known field absent from the payload SHALL take its declared
default.

A kind the decoding build does not know SHALL be recorded and skipped, and the consumer SHALL continue to
the next message rather than reporting that nothing arrived. Reporting nothing arrived ends the
consuming component's drain for that pass, so an unknown kind would stall every message behind it.

The free-form mappings a message carries SHALL be restricted to values a wire format carries — strings,
numbers, booleans, null, and lists or mappings of those — and the publisher SHALL refuse anything else at
the hand-over, where the call site still names the producer, rather than coercing it. Every publisher
SHALL apply that check, not only the one that serializes: a producer whose mapping holds a path or a
datetime must fail in the process that put it there, not on the day a wire is underneath.

#### Scenario: A message returns as its own kind

- **WHEN** a message is published through a serializing transport and polled back
- **THEN** the polled value is an instance of the kind that was published
- **AND** it equals the published message, including its nested parts

#### Scenario: The scope returns as an owner scope

- **WHEN** any message is serialized and deserialized
- **THEN** the decoded scope is an `OwnerScope` whose four axes equal the original's
- **AND** a payload whose scope is missing an axis or carries an extra one fails to decode

#### Scenario: A field of the wrong type fails to decode

- **WHEN** a payload declares a boolean field as a string, or a mapping field as a string
- **THEN** decoding raises rather than returning a message carrying the wrong object

#### Scenario: A field a reader does not know does not stop delivery

- **WHEN** a payload encoded by a newer instance carries a field the decoding instance does not know
- **THEN** the unknown field is ignored and the message is delivered
- **AND** a known field absent from the payload takes its declared default

#### Scenario: An unknown kind does not stall the drain

- **WHEN** a message naming a kind the decoding instance does not know is read
- **THEN** it is recorded and skipped, and the next message is returned
- **AND** the consumer does not report that nothing arrived

#### Scenario: A value no wire format carries is refused at the hand-over

- **WHEN** a message whose free-form mapping holds a filesystem path, a datetime, a set or raw bytes is
  published
- **THEN** publishing raises and nothing is queued
- **AND** the error names the field and the path within it, so the producer is identifiable from the call
  site

#### Scenario: The in-process transport applies the same check

- **WHEN** the in-process publisher is handed a message with an unsupported value in a free-form mapping
- **THEN** it refuses it exactly as a serializing transport would, so a producer cannot pass in-process
  and fail across a process boundary
