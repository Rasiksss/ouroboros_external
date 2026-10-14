# background-memory-events Specification

## Purpose

How events reach the background memory handlers: the shared in-memory transport that carries every
background-memory event kind, and the runtime loop that polls it. The loop dispatches by event type
through a composition-root-supplied mapping and never names a handler; a handler may ask for one
delayed re-delivery that the loop delivers itself, never through the transport.
## Requirements
### Requirement: The runtime loop dispatches by event type

The runtime loop SHALL hold a mapping from event type to the callable that handles it, supplied by
the composition root, and SHALL dispatch each polled event through that mapping. The loop SHALL NOT
import any handler, name any handler type, or branch on event type in its own body. An event whose
type is absent from the mapping SHALL be logged as a warning and skipped.

The loop SHALL NOT run a handler in its own body of execution. Each delivery SHALL be started as a
task the loop owns, and the loop SHALL return to polling without waiting for it. A loop that runs
handlers inline is sequential however concurrent the rest of the system is: one delivery that spans
several model calls holds every other owner's work for its whole duration.

The loop SHALL own every task it starts: it SHALL hold a reference to each while it runs, SHALL
collect what each raised, and SHALL log a failed delivery with its traceback. A delivery that raises
SHALL fail its own task and SHALL NOT end the loop or affect another delivery.

Every composition root that builds this mapping SHALL register the same set of kinds. The table is
composed twice — by the single-process runtime (`server.py`) and by the tier that runs the
loop as a process of its own (`multiuser_config/background_memory/dependencies.py`) — from two
literals that nothing compares, so a kind added to one and forgotten in the other is not a failure
but a warning on a log nobody reads, in one deployment mode only. The skip rule above is the fallback
for an instance reading a kind a newer producer published, not a licence for two roots of one build
to disagree.

#### Scenario: Two event kinds reach two different handlers

- **WHEN** events of two registered kinds are polled
- **THEN** each reaches the callable registered for its own type, unchanged

#### Scenario: The loop names no handler

- **WHEN** the loop module's imports are inspected
- **THEN** it imports no handler module

#### Scenario: An unregistered event kind is skipped

- **WHEN** an event whose type has no entry in the mapping is polled
- **THEN** a warning names the event type and the loop continues polling

#### Scenario: Every root registers the same kinds

- **WHEN** the dispatch tables the composition roots of one build compose are compared
- **THEN** they name the same set of event kinds
- **AND** a kind added to one of them and not the other fails the comparison

#### Scenario: A slow delivery does not hold another owner's work

- **WHEN** a delivery for one ownership pair is still running and an event for a different pair is
  published
- **THEN** the second event is polled and dispatched while the first is still running

#### Scenario: A failing delivery does not end the loop

- **WHEN** a delivery raises
- **THEN** the failure is logged with its traceback
- **AND** the loop continues polling and other deliveries are unaffected

#### Scenario: The loop owns every task it started

- **WHEN** the loop is inspected while deliveries are running
- **THEN** it holds a reference to each
- **AND** no delivery task is started without one

### Requirement: A handler may request one delayed re-delivery

A handler SHALL return either nothing, meaning it is done, or a follow-up naming an event to deliver
and a delay before delivering it. The loop SHALL deliver a due follow-up itself rather than
publishing it, so a follow-up SHALL NOT be subject to the transport's capacity or its drop rule. A
follow-up with no delay SHALL be delivered on the next pass of the loop, so the stop signal is
re-checked between deliveries. The loop SHALL fold the earliest pending follow-up's due time into its
poll timeout rather than sleeping.

The loop SHALL be able to hold more than one pending follow-up: at most one per event kind and scope,
where a later request for the same kind and scope replaces the earlier one. A follow-up requested for
one scope SHALL NOT displace one outstanding for another. Nothing bounds the collection but the
handlers themselves — a handler that runs one unit of work per scope produces at most one outstanding
follow-up per scope.

A follow-up SHALL be dispatched on the same terms as a polled event: as a task the loop owns, and
subject to the same serialization per ownership pair. A follow-up is the continuation of a run, so it
SHALL NOT be started while another delivery for its pair is still running.

A follow-up whose delivery cannot be dispatched — no handler is registered for its kind, or the
handler raises — SHALL NOT be rescheduled, so a failing handler is never retried into a loop. A
handler that holds state between deliveries is therefore responsible for releasing that state before
it returns, since no further delivery will reach it.

#### Scenario: An immediate follow-up is delivered promptly

- **WHEN** a handler returns a follow-up with no delay
- **THEN** the named event is delivered on the loop's next pass

#### Scenario: A delayed follow-up waits out its delay

- **WHEN** a handler returns a follow-up with a delay
- **THEN** the named event is not delivered before the delay elapses, and is delivered after it

#### Scenario: A follow-up survives a full transport

- **WHEN** the transport is at capacity and a handler returns a follow-up
- **THEN** the follow-up is still delivered

#### Scenario: Shutdown during a pending follow-up ends the loop

- **WHEN** the stop signal is set while a follow-up is waiting out its delay
- **THEN** the loop returns without delivering it

#### Scenario: A pending follow-up does not spin the loop

- **WHEN** a follow-up is pending and the transport is empty
- **THEN** the loop waits rather than polling continuously

#### Scenario: One scope's follow-up does not displace another's

- **WHEN** a follow-up is outstanding for one scope and a handler returns a follow-up of the same
  kind for a different scope
- **THEN** both are delivered, each after its own delay

#### Scenario: The earliest pending follow-up bounds the wait

- **WHEN** several follow-ups are pending with different due times
- **THEN** the loop's next wait is no longer than the time until the earliest of them

#### Scenario: A repeated request for one scope does not accumulate

- **WHEN** a handler returns a follow-up of the same kind and scope on each of several deliveries
- **THEN** at most one delivery for that kind and scope is outstanding at any time

#### Scenario: A due follow-up waits for its pair to be free

- **WHEN** a follow-up for an ownership pair becomes due while another delivery for that pair is
  still running
- **THEN** it is not started until that delivery has finished

### Requirement: Runtime-internal events never cross the transport

An event kind that exists only to re-deliver work within the loop SHALL derive from the same base
message type, so it can be registered in the dispatch mapping, but SHALL NOT be published by any
producer. A producer-side module SHALL name only the event kinds it publishes.

Where a transport has a wire format, that format SHALL be the enforcement point rather than the
convention: the codec SHALL have no wire form for a runtime-internal kind, and encoding one SHALL
raise. A registry derived by reflection over the declared kinds SHALL NOT be used for this, because it
would enrol the runtime-internal kind automatically and silently undo the rule.

#### Scenario: No producer publishes a continuation

- **WHEN** every publishing call site is inspected
- **THEN** none constructs an event kind reserved for re-delivery

#### Scenario: A continuation has no wire form

- **WHEN** the kind reserved for re-delivery is handed to the wire codec
- **THEN** encoding raises rather than producing a payload a broker could carry

### Requirement: The in-memory transport is bounded and drops on overflow

The in-memory transport SHALL be bounded by a fixed capacity, shared by every event kind that
crosses it. Publishing into a full transport SHALL drop the event and log a warning naming the
capacity, and SHALL NOT block the publishing thread, raise, or evict an earlier event. The warning
SHALL name the source task for kinds that carry one, and SHALL still be emitted for kinds that do
not. The capacity SHALL be chosen so that the most frequent event kind cannot routinely crowd out a
kind whose payload cannot be regenerated.

This capacity governs the in-process transport alone. It is sized against one handler run occupying a
loop that is about to drain the queue, which is not the question a broker's backlog asks, so a
transport whose consumer runs in another process SHALL declare its own bound under the requirement
that governs broker-backed transports rather than reusing this one.

Dropping SHALL remain the answer for both, for the same asymmetry: the frequent kind's payload is
regenerated by the next event and re-read from the store, while the publishing thread is a producer
finishing a user's work and must not be held up by memory work.

#### Scenario: Publishing into a full transport drops the newest event

- **WHEN** the transport is at capacity and another event is published
- **THEN** the call returns without raising
- **AND** a warning names the cap and the source task id
- **AND** the events already queued are still delivered in order

#### Scenario: A kind without a source task still warns

- **WHEN** the transport is at capacity and an event kind that names no source task is published
- **THEN** the call returns without raising and a warning names the cap

#### Scenario: One kind's burst does not exhaust the capacity

- **WHEN** the capacity is compared against the number of events the most frequent kind can produce
  while one handler run occupies the loop
- **THEN** the capacity leaves room for the other kinds

#### Scenario: The two transports declare two capacities

- **WHEN** the in-process and broker-backed transports are compared
- **THEN** each declares its own capacity, and neither reuses the other's

### Requirement: Publishing and consuming are separate interfaces

The background memory transport SHALL be declared as two abstract interfaces: a publisher exposing
`publish(event)` and a consumer exposing an awaitable `poll(timeout)` and an awaitable `close()`,
which releases whatever that consumer holds on the broker between polls. A composition root SHALL
reach that release through the interface and SHALL NOT decide it by inspecting the concrete
transport it built. A producer-side caller SHALL
depend on the publisher interface only, and the worker-side loop SHALL depend on the consumer
interface only. Neither interface SHALL name a queue, a topic, a broker, or any other transport in
its signatures. Concrete transports SHALL subclass these ABCs, and an implementation missing an
abstract member SHALL NOT be instantiable.

The two halves SHALL differ in this: consuming is awaitable and publishing is not. The asymmetry is
deliberate and SHALL NOT be resolved by making both alike. A publisher is called from whatever thread
finished a user's work, and the contract it owes that caller is that it never blocks it, which a
synchronous fire-and-forget publish already satisfies; making it awaitable would oblige every
producer to be asynchronous for nothing. A consumer is the loop's idle state, and a blocking one is
the loop's whole lifetime.

#### Scenario: The agent's dependency cannot consume

- **WHEN** the object the agent holds for reporting completed tasks is inspected
- **THEN** it satisfies the publisher interface
- **AND** it exposes no method for reading events back

#### Scenario: The worker's dependency cannot publish

- **WHEN** the object the memory runtime loop holds for reading work is inspected
- **THEN** it satisfies the consumer interface
- **AND** it exposes no method for publishing events

#### Scenario: A consumer that cannot be released is rejected

- **WHEN** a consumer implementation omits `close`
- **THEN** it cannot be instantiated

#### Scenario: An incomplete implementation is rejected

- **WHEN** a class claims to implement either interface but omits an abstract member
- **THEN** it cannot be instantiated

#### Scenario: Consuming is awaitable and publishing is not

- **WHEN** the two interfaces are inspected
- **THEN** the consumer's poll is declared as a coroutine
- **AND** the publisher's publish is not, and no producing call site awaits it

### Requirement: The transport delivers messages unchanged

A transport implementation SHALL deliver what it was given and nothing else. It SHALL NOT apply
eligibility rules, truncate fields, convert a message into another type, read settings, or read the
task store. A message published SHALL be returned by a poll as the same value, with the same nested
message parts.

#### Scenario: A published event round-trips intact

- **WHEN** an event is published and then polled from the same transport
- **THEN** the polled value equals the published event, including its nested task reference

#### Scenario: The transport does not reach the business layer

- **WHEN** the transport module's imports are inspected
- **THEN** it imports neither the eligibility and request-building module nor the handler

### Requirement: Messages are named types under one base

Every message crossing the transport SHALL be a named frozen dataclass, not a dictionary. All such
messages SHALL derive from one base message type, and the consumer's `poll` SHALL be typed to that
base so a second kind of memory event can be introduced without changing either interface. What a
publisher knows about a completed task SHALL be carried by a single named reference type rather than
loose parameters.

Every message SHALL name the `OwnerScope` of whoever caused it. That field SHALL be declared once,
on the base message type, and SHALL be required with no default: a kind added later cannot forget to
say whose it is, and a transport can read identity off any message without naming a single kind — a
partition key is the one thing a broker needs and the one thing the two interfaces must not learn a
kind to obtain. A runtime-internal kind SHALL carry it too, because a re-delivery must name the work
it resumes rather than assume it.

Every message SHALL also name the operation it belongs to, by carrying the operation identifier. That
field SHALL be declared once, on the base message type, for the same reason the scope is: a kind added
later cannot forget it, and the dispatch loop can read it off any message without naming a kind. Unlike
the scope it SHALL default to empty, because a message may describe work that no operation triggered,
and the consumer decides what an absent identifier means rather than the publisher being unable to
construct the message. It SHALL be carried as a field rather than left to a context variable: a message
crosses a thread, and a context variable does not.

The operation identifier SHALL NOT be treated as identity. Whose work a message describes is the
scope; which operation caused it is the identifier; they answer different questions and neither
substitutes for the other.

No message SHALL carry a chat id, an owner id, or any other identity field beside the scope. Two
spellings of identity on one message is how they drift.

A kind whose work concerns one conversation SHALL therefore name that conversation through the chat
axis of its scope, and SHALL be published under a scope minted for that conversation. The rule above
already forbids the second spelling of identity; what this adds is that the first is now
load-bearing. Until chat history was keyed by chat, this axis reached no predicate on the message
path at all — `transport/journal.py` reads messages under a scope whose chat axis is a literal
service-job name, and `new_core/identity/service_scope.py` mints one whose docstring promises
nothing reads it — so what a memory event carried there cost nothing. It costs a title now. No axis
is ever empty (`new_core/identity/owner_scope.py`), so a scope that does not name the conversation
does not fail to construct: it carries something else in that slot, reaches a handler that titles
whatever it was told to, and names no error on the way.

#### Scenario: A second event kind needs no interface change

- **WHEN** a new memory event type is added as a subclass of the base message type
- **THEN** neither the publisher interface nor the consumer interface changes to carry it

#### Scenario: Consumers reject an unknown event kind without failing

- **WHEN** a consumer returns an event whose type the loop has no handler for
- **THEN** the loop logs a warning naming the event type
- **AND** it continues polling

#### Scenario: A message cannot be constructed without a scope

- **WHEN** any message kind is constructed with no scope
- **THEN** construction raises

#### Scenario: Identity is declared once, on the base

- **WHEN** the message types are inspected
- **THEN** the scope is declared on the base type
- **AND** no kind declares a chat id, an owner id, or any other identity field of its own

#### Scenario: A runtime-internal kind carries a scope too

- **WHEN** the kind reserved for re-delivery is inspected
- **THEN** it carries a scope, so a re-delivery names the work it resumes

#### Scenario: The operation identifier is declared once, on the base

- **WHEN** the message types are inspected
- **THEN** the operation identifier is declared on the base type
- **AND** no kind declares an operation identifier of its own

#### Scenario: A message can be constructed without an operation identifier

- **WHEN** any message kind is constructed naming only its scope
- **THEN** construction succeeds and the operation identifier is empty

#### Scenario: A publisher inside an operation stamps that operation

- **WHEN** a publisher publishes a message while an operation is active
- **THEN** the message carries that operation's identifier

#### Scenario: A per-conversation kind names its conversation through the scope

- **WHEN** a kind whose work concerns one conversation is inspected
- **THEN** it declares no chat id of its own
- **AND** the conversation it concerns is the chat axis of the scope it was published under

#### Scenario: A per-conversation kind is not published under a service scope

- **WHEN** the call sites that publish such a kind are inspected
- **THEN** none publishes under a scope minted for a service job, whose chat axis holds that job's
  name rather than a conversation
- **AND** each names the conversation whose record was created

### Requirement: Polling names a timeout

The consumer interface SHALL accept a timeout on `poll`, defaulting to non-blocking. A caller SHALL
NOT need its own sleep to avoid spinning, and a poll that finds nothing within the timeout SHALL
return nothing rather than raising.

A poll SHALL NOT occupy the thread it is awaited on while it waits. Whatever the backend, waiting for
an event and waiting out a timeout SHALL yield, so that other work sharing the caller's event loop
runs during the wait. This applies to the in-process backend as much as to a broker-backed one: a
queue read that blocks a thread blocks every coroutine on it, and the in-process transport is the
backend the default profile runs.

A backend whose client is synchronous SHALL NOT satisfy this by moving each poll onto a thread of its
own. A thread per poll is the arrangement this seam is being taken out of, and an abandoned poll
holds its thread for as long as the underlying read runs.

#### Scenario: An empty transport returns nothing immediately

- **WHEN** `poll` is called with no timeout on an empty transport
- **THEN** it returns nothing without blocking and without raising

#### Scenario: A waiting poll returns nothing after its timeout

- **WHEN** `poll` is called with a timeout on an empty transport
- **THEN** it returns nothing after approximately that timeout

#### Scenario: A waiting poll does not stall its neighbours

- **WHEN** a poll is awaiting an event on an empty transport and other work is scheduled on the same
  event loop
- **THEN** that other work runs while the poll waits

#### Scenario: An in-process poll yields like any other

- **WHEN** the in-process transport's consumer is awaited on an empty queue and other work is
  scheduled on the same event loop
- **THEN** that other work runs while the poll waits
- **AND** the consumer did not start a thread to achieve it

### Requirement: The publisher is resolved from a process-local registry

The composition root SHALL install the publisher into a process-local registry, and code that cannot
receive it as a constructor argument SHALL read it from there. The registry's default SHALL be a
no-op publisher, so a process that never installs one still runs, dropping events silently. The
registry SHALL be resettable for tests.

The registry SHALL offer exactly two initializers: one for the in-process transport, which builds its
publisher over the queue it is given, and one for a broker backend, which installs a publisher already
built by the root that names the broker, whichever broker backend built it. The broker initializer
SHALL NOT be named after a particular broker, and a further broker backend SHALL NOT add an
initializer of its own: the root that names that broker builds its publisher and installs it through
the same one. Neither initializer SHALL change the default: the no-op publisher stays what an
uninitialized process has.

Which half of the seam a process installs SHALL follow from what that process does. A process that
produces these events — one that completes tasks, or one that persists chat rows — SHALL install a
publisher. A process that only consumes them SHALL leave the default in place, because installing a
publisher there would only mean that process talking to itself.

A root that installs a publisher SHALL install it before it composes anything that reads the registry,
since a collaborator handed the publisher at construction holds whatever was installed at that moment.

#### Scenario: The default publisher accepts and discards

- **WHEN** an event is published before any publisher is installed
- **THEN** the call succeeds and no event is delivered anywhere

#### Scenario: The installed publisher is what callers get

- **WHEN** the composition root installs a publisher and a caller then reads the registry
- **THEN** the caller receives the installed publisher

#### Scenario: A broker publisher is installed as it was built, whichever broker built it

- **WHEN** a root installs a publisher it built for the broker the selector names
- **THEN** it does so through the one broker initializer, which takes the built publisher and nothing
  else, and the registry answers with that same object
- **AND** the registry offers no initializer named after a particular broker

#### Scenario: A second initializer leaves the default alone

- **WHEN** a process initializes neither the in-process nor the broker publisher
- **THEN** the registry still answers with the no-op publisher

#### Scenario: The consuming process installs no publisher

- **WHEN** the process that runs the memory loop finishes composing itself
- **THEN** the registry there still holds the no-op publisher

#### Scenario: A producer root installs before it composes readers

- **WHEN** a producer root composes a collaborator that takes the publisher as a constructor argument
- **THEN** the publisher was installed before that collaborator was built

### Requirement: The background memory package depends on neither the supervisor nor the agent

No module in the background memory package SHALL import the supervisor package or the agent package,
statically or lazily. The dependency direction SHALL be one-way: the agent and the composition root
may import the background memory package.

The package SHALL also not import the persistence package. It owns its own repositories, so a call
reaching back into a store another component owns is a second path to the same rows with different
semantics and a different engine. Two exceptions SHALL be allowed, each named individually rather
than by relaxing a prefix:

- the field-encryption module, which is imported rather than duplicated because a second
  implementation of at-rest protection is a security surface with two answers;
- pure domain modules that perform no input or output and hold no mutable global state, which are
  policy the package would otherwise copy and let drift.

The allowlist SHALL be a list of modules, not a prefix, and adding to it SHALL be a decision recorded
where the package's rules are recorded rather than a line added to a test.

The package SHALL create no thread. It SHALL import no threading facility and construct no thread,
with one exception, named at the single module that holds it: the per-call executor that takes a
synchronous tool body off the event loop, which exists because a tool body is synchronous by nature
and because placing it on a shared pool lets one hung body starve later calls. That exception SHALL
NOT be generalised, and a second use of it SHALL fail the check rather than extend it.

No module of the package SHALL perform a blocking call from a coroutine.

Every one of these rules SHALL be checked by parsing the package rather than by importing it. The
package legitimately imports modules that pull a forbidden root in transitively, so a check made over
loaded modules would go red for a rule that is not broken.

#### Scenario: Boundary violations are detected without importing the package

- **WHEN** every module of the package is parsed and its import statements inspected
- **THEN** none names the supervisor package or the agent package as an import root

#### Scenario: The persistence package is reachable only through the named exceptions

- **WHEN** every module of the package is parsed
- **THEN** any import of the persistence package names one of the individually listed exceptions
- **AND** the list is of modules, not of a prefix

#### Scenario: The package creates no thread

- **WHEN** every module of the package is parsed
- **THEN** none imports a threading facility or constructs a thread, except the single named module
  that owns the per-call executor for a tool body

#### Scenario: A second per-call executor is refused

- **WHEN** a module other than the named one constructs an executor
- **THEN** the check fails rather than the allowance being extended

#### Scenario: No coroutine of the package blocks

- **WHEN** every module of the package is parsed
- **THEN** no coroutine performs a synchronous database, HTTP or broker call, sleeps, or waits on a
  threading primitive

### Requirement: The runtime loop establishes the operation before dispatch

The runtime loop SHALL establish the operation identifier around every delivery, reading it from the
message and never from ambient context. Deliveries run concurrently as tasks of their own, so an
identifier SHALL be established per delivery and SHALL NOT be shared between two that overlap. No
context a publisher established ever reaches a handler on its own.

The loop SHALL carry the message's identifier when it has one, and SHALL generate one when it does
not, so that no work the loop dispatches ever runs without an operation. Generating rather than
running unidentified is deliberate: a handler makes outbound calls, and an outbound client omits the
header rather than failing when no identifier is active — an unidentified delivery would therefore
lose correlation silently rather than visibly.

The scope SHALL cover the whole delivery, so that a handler's own log records and every outbound
request it makes carry the identifier. The loop SHALL do this without naming any event kind: the
identifier is read off the base type, exactly as the dispatch key is.

The scope SHALL be released when the delivery returns, whether it returned a follow-up, returned
nothing, or raised — a handler that fails SHALL NOT leak its operation onto the loop's next poll or
onto another delivery running beside it.

#### Scenario: A handler runs under the publisher's operation

- **WHEN** a message carrying an operation identifier is delivered
- **THEN** the handler runs with that identifier active
- **AND** a log record the handler emits carries it
- **AND** an outbound request the handler makes carries it in the `X-Trace-Id` header

#### Scenario: A message with no operation identifier still runs under one

- **WHEN** a message with an empty operation identifier is delivered
- **THEN** the handler runs with a generated identifier active
- **AND** that identifier is well formed

#### Scenario: The loop reads the identifier without naming a kind

- **WHEN** the loop module is inspected
- **THEN** it reads the operation identifier from the base message type
- **AND** it branches on no event kind to obtain it

#### Scenario: A failing delivery does not leak its operation

- **WHEN** a handler raises during a delivery
- **THEN** the loop continues polling with no operation active

#### Scenario: Two concurrent deliveries keep separate operations

- **WHEN** two deliveries carrying different operation identifiers run at the same time
- **THEN** each handler runs under its own identifier
- **AND** a log record either emits carries its own, not the other's

### Requirement: A re-delivery request continues the operation that asked for it

The event a handler returns as a follow-up SHALL carry the operation identifier the handler was
running under, so that a run spanning several deliveries is one operation from trigger to commit. A
follow-up is one run handing itself back, not new work.

A run SHALL NOT fragment across identifiers: every delivery of a multi-batch run SHALL report the
same operation identifier as the delivery that started it.

#### Scenario: A continued run keeps one identifier

- **WHEN** a handler returns a follow-up and the loop re-delivers it
- **THEN** the re-delivery runs under the same operation identifier as the delivery that produced it

#### Scenario: A run spanning several batches reports one operation

- **WHEN** a run completes over three deliveries
- **THEN** all three report the same operation identifier

### Requirement: A broker-backed transport bounds what it hands the broker, waits out each publish on its own and states its deviation

A transport whose consumer runs in another process SHALL bound what it retains in the broker, and
SHALL do so with the broker's own bound rather than a client-side check: a read-then-write check
races the next publisher and costs a second round trip on the producing thread.

The bound SHALL be its own capacity, sized for a broker holding a stalled tier's backlog, and SHALL
NOT be the in-process transport's capacity, which is sized for a queue a thread in the same process is
about to drain. The two answer different questions and may be stated in different units, so neither
SHALL be required to exceed the other.

The bound MAY be a count of entries applied by the append, or the broker's own retention by size or
by age. A backend whose bound is retention SHALL state its granularity and its timing: what unit the
broker evicts, over what scope the size is counted, and when eviction runs. Retention is enforced
after the append and in whole units, so the retained size can sit above the bound until the next
eviction. A retention bound SHALL be configured so that eviction can reach it. A bound that the unit
still being written can never satisfy is no bound.

Where the broker's bound evicts the oldest entry rather than refusing the newest, the backend SHALL
state that deviation in the architecture document that describes it. The requirement that a drop be
logged with its capacity SHALL NOT carry over: the broker does not report what an eviction removed,
and a warning that names no discarded event is worse than none. Consumer-group lag, the distance from
the group's recorded position to the newest event, is what reports a backlog. The oldest position the
broker still retains is what reports that eviction has reached events the group had not read. How
much the broker retains SHALL NOT be read as a backlog, because events a group has already read stay
retained until the bound evicts them.

Publishing SHALL NOT raise into the producing turn. A broker error, a payload the wire format cannot
carry, or a payload larger than the broker accepts SHALL be logged as a warning naming the event kind
and the scope. The event SHALL be dropped, the same outcome a full transport already produces. A
producer publishing without a guard of its own SHALL NOT be able to fail its task because a memory
event could not be handed over. A payload over a message-size ceiling the backend knows SHALL be
refused before any request reaches the broker.

Every step of a publish that can block SHALL carry an explicit bound: connecting, waiting for the
broker's metadata, appending, and waiting for the broker's acknowledgement. "Never blocks the calling
thread" cannot mean "performs no I/O" for a broker; it can only mean bounded. The bound SHALL be
stated by the one description of the broker's client or by the backend itself, and SHALL NOT be left
to a library default.

Where an append handed to the broker's client cannot be withdrawn once the wait for its
acknowledgement expires, the backend SHALL state that deviation. The publish returns and warns that
the event was dropped, yet the broker MAY still accept the append later, and the tier then handles
the event once. The warning therefore records an event that was not acknowledged in time, not one
proven lost.

Each publish SHALL wait out its own bounds, and a backend SHALL NOT remember an earlier failure in order
to skip a later wait: while the broker does not answer, every publish waits for its bounds and drops its
event with the warning. The cost SHALL be stated where the backend is described: a producing thread that
publishes many events during an outage waits once per event.

#### Scenario: An overflowing broker is bounded rather than grown

- **WHEN** more events are published than the broker-backed transport's bound admits
- **THEN** once the broker's eviction has run, what it retains is bounded at approximately that bound
- **AND** the publishing calls return without raising

#### Scenario: A retention bound is one eviction can reach

- **WHEN** the broker-backed transport's bound is the broker's retention
- **THEN** it is configured by size and by age, with a unit of eviction small enough that the bound
  takes effect
- **AND** the backend's granularity and timing are stated in the architecture document

#### Scenario: The broker capacity is not the in-process capacity

- **WHEN** the two transports' capacities are compared
- **THEN** each is declared separately, and neither reuses the other's constant

#### Scenario: The eviction direction is declared

- **WHEN** the architecture document describing the broker-backed transport is read
- **THEN** it states that its bound evicts the oldest entry, unlike the in-process bound

#### Scenario: A broker failure does not fail the producing turn

- **WHEN** the broker is unreachable and a producer that wraps nothing publishes an event
- **THEN** the call returns without raising and a warning names the event kind and the scope

#### Scenario: A payload the wire format cannot carry is dropped, not raised

- **WHEN** an event carries a free-form field holding a value no wire format represents
- **THEN** publishing returns without raising and a warning names the event kind and the scope

#### Scenario: A payload over the broker's size ceiling is dropped before it is sent

- **WHEN** the broker has a message-size ceiling and an event's wire form exceeds it
- **THEN** publishing returns without raising and a warning names the event kind and the scope
- **AND** no append reaches the broker

#### Scenario: Every blocking step of a publish is bounded

- **WHEN** the broker does not answer and an event is published
- **THEN** the call returns within the sum of the stated bounds of its steps

#### Scenario: An append whose acknowledgement came too late is declared

- **WHEN** a publish returned with a warning because the broker did not acknowledge the append
  within its bound, and the broker accepts that append afterwards
- **THEN** the event is delivered to the tier once
- **AND** the backend's architecture document states that such a warning does not prove the event lost

#### Scenario: Every publish during an outage waits out its own bounds

- **WHEN** the broker does not answer and several events are published one after another
- **THEN** each call returns within the sum of the stated bounds of its steps, without raising
- **AND** a warning names each event's kind and scope

### Requirement: Messages crossing a broker have a wire form that rebuilds their type

A broker-backed transport SHALL encode and decode messages through one codec, shared by every backend
of this seam and living beside the transports rather than in the interface module — the interfaces
name no wire format.

The wire form SHALL carry a tag naming the concrete message kind, because the consumer is typed to the
base message and a reader that received a bare mapping would have to guess. Decoding SHALL reconstruct
through the message constructors, so that scope validation and the nested task reference are rebuilt as
the declared types rather than as equal collections of plain values.

A key the reading build does not know SHALL be dropped rather than refused, so an instance running an
older build does not discard events a newer producer published. A known field that decodes to a value
of the wrong declared type SHALL be refused: a wrong object delivered silently is worse than a loud
failure on the one hop where nobody is watching.

The codec's registry of kinds SHALL be written out explicitly rather than derived by reflection over
the declared message types, so that a kind can be deliberately absent from it.

#### Scenario: An event round-trips through the wire form as its own type

- **WHEN** an event with a nested task reference is encoded and decoded
- **THEN** the result equals the original, is an instance of the same concrete kind, and its scope and
  nested reference are the declared types rather than mappings

#### Scenario: An unknown key does not lose the event

- **WHEN** a wire form carries a field the reading build does not declare
- **THEN** the event decodes without it rather than being refused

#### Scenario: A field of the wrong type is refused

- **WHEN** a wire form carries a numeric field as a string
- **THEN** decoding raises rather than returning an event with a wrong-typed field

#### Scenario: The registry is explicit

- **WHEN** the codec's mapping from wire tag to message kind is inspected
- **THEN** it is written out kind by kind and is not derived from the declared message types

### Requirement: A broker-backed consumer competes in one group, begins at the present and acknowledges before it dispatches

Every instance of the tier that consumes these events SHALL read through one shared consumer group, so
that an event is handled once however many instances run.

A group that has never recorded a position SHALL begin at the present rather than replaying what
accumulated before the tier existed. On a broker that divides a stream into parts and records a
position per part, each part without a recorded position SHALL begin at the present when an instance
first reads it, and no deployment step SHALL fix that start. The cost SHALL be stated where the backend
is described: a part that moves to another instance before any position was recorded on it begins at the
present again there, and the events published to it in between are skipped, never replayed.

A group whose recorded position the broker's bound has since evicted SHALL resume without failing its
reads. A broker that keeps the group's place as the last entry it delivered SHALL resume at the oldest
event still retained, and SHALL NOT skip everything retained after that position. A broker whose
position is an offset per part SHALL treat an evicted offset as no position and resume that part at the
present; the events retained after the evicted position are skipped, and that cost SHALL be stated where
the backend is described.

A broker may discard a group's recorded positions once the group has stayed empty for longer than
the broker's own offset retention, while keeping the stream. Such a group SHALL begin at the present
again, as a group that never recorded a position does, and no tier SHALL refuse its start because the
positions are missing. The cost SHALL be stated where the backend is described: events published while
the positions were missing are skipped, never replayed, and no event the tier handled before it stopped
is delivered again.

Consumption SHALL resume, without restarting the process, after the broker loses or recreates its
state (the group, the stream, or the group's recorded position), rather than failing every read from
then on. The polling loop treats a failed read as one bad poll and polls again. A consumer that does
not resume has not merely lost the events published during the outage: its tier has stopped
consuming, and it reports that only as a repeated warning. Where the broker keeps the group and its
position across a restart, the consumer resumes where the group left off. Where the broker loses the
group together with the stream's events, the consumer recreates the group in place, at the present.
Where the broker keeps the stream and discards only the group's positions, which it does only while
the group has no member, the consumer begins at the present, as stated above for a group whose
positions were discarded. Resuming SHALL NOT be read as recovering anything: what a
broker running without persistence lost is gone, as this stage accepts.

The consumer SHALL record its position past an event before returning that event to the loop.
Recording it afterwards would require the consumer interface to expose an acknowledgement, which it
SHALL NOT do. It would also leave events that only a reclaiming job could recover, which is deferred
work with no owner. The loop already treats a failed handling as a lost event rather than a retry, so
redelivery would contradict it, and a redelivered distillation would re-run work that writes memory.

A position the broker did not record SHALL NOT withhold the event already in hand: the event SHALL
still be returned, and what was not recorded SHALL be settled without handing that event to the loop
again. A backend with a pending list per entry settles it on a throttled pass that acknowledges and
never re-reads, and that pass SHALL never be the reason a read fails. A backend whose position is one
offset per part of the stream SHALL record the position again before it reads anything further. That
keeps at most one event unrecorded at a time. A recording the broker refuses because the consumer no
longer holds the part in the group's current generation, whether the part moved to another instance,
a rebalance is under way, or the consumer's own membership lapsed, SHALL be dropped rather than
retried, so a position is never moved backwards under a newer owner.

The cost SHALL be stated. An instance that dies between recording the position and handling the event
loses that event. On a backend whose position is per part of the stream, a failed recording followed
by the part being reassigned before the recording is retried, to another instance or back to the same
consumer after a rebalance or a lapse of its membership, MAY deliver that one event a second time.

An entry that cannot be decoded, and an entry whose kind tag the reading build does not know, SHALL be
acknowledged and skipped with a warning naming the entry, never left to block the group or be
redelivered forever. Decoding SHALL happen inside the backend after the position is recorded. It
SHALL NOT happen in a client hook that runs before the position can advance, so a malformed entry
cannot stall the part of the stream it sits in. One read SHALL skip only a bounded number of such
entries before it returns to the loop, whose stop signal is re-checked between reads.

The group SHALL NOT be assumed to preserve ordering between instances, nor to deliver one scope's
events to one instance. A consumer of this transport SHALL therefore hold no state that requires
either.

Reading with no timeout SHALL NOT wait for events, and reading with a timeout SHALL wait for events
for at most that timeout. A timeout of zero SHALL NOT be handed to the broker as a blocking read that
never returns: the polling loop re-checks its stop signal only between reads, so an unbounded read is
a process that does not shut down. A backend whose client fetches in the background MAY make one
fetch, bounded by a small fixed interval, for a zero timeout, so that an event already published is
returned rather than left for a later read. Every other request a read makes to the broker SHALL
carry an explicit bound of its own, so a read against a broker that does not answer returns within
its timeout plus that bound.

#### Scenario: Two instances of the tier share the work

- **WHEN** two consumers of the tier are reading and one event is published
- **THEN** exactly one of them receives it

#### Scenario: A fresh group does not replay history

- **WHEN** the group's start is established after events have already been published
- **THEN** the consumer receives only events published from that point on

#### Scenario: A part of the stream without a recorded position begins at the present when first read

- **WHEN** events are published to a part of the stream on which no position is recorded, after an
  instance that held it stopped without reading them, and the part moves to another instance
- **THEN** the instance that receives the part delivers only the events published after it began reading
  that part
- **AND** the description of the backend states that the events published in between are skipped

#### Scenario: An evicted position resumes without failing reads

- **WHEN** the broker's bound has evicted the events at and after the group's recorded position
- **THEN** the next read returns without raising
- **AND** a broker that keeps the group's last delivered entry delivers the oldest event still retained,
  while a broker whose position is an offset per part delivers the next event published after the read

#### Scenario: A group whose positions the broker discarded begins at the present

- **WHEN** the broker has discarded the recorded positions of a group that stayed empty past its offset
  retention, the stream is still retained, and a tier starts
- **THEN** the start is not refused
- **AND** the group's consumers receive only events published after they began reading, none published
  while the positions were missing and none the tier handled before it stopped
- **AND** the description of the backend states that the events published while the positions were
  missing are skipped

#### Scenario: Consumption resumes after the broker loses or recreates its state

- **WHEN** the broker has restarted, losing the group with its state or keeping it, and the consumer reads
- **THEN** the read returns without raising, and an event published after the restart is delivered
- **AND** the consumer does not fail every subsequent read

#### Scenario: The position is recorded before the event is returned

- **WHEN** an event has been returned by a read
- **THEN** the group's recorded position is already past it
- **AND** on a backend with a pending list, no entry for it is pending

#### Scenario: An unrecorded position still delivers the event

- **WHEN** recording the position past an event fails, and the consumer still holds that part of the
  stream when the recording is retried
- **THEN** the event is still returned
- **AND** the unrecorded position is settled later without the event being handed to the loop again

#### Scenario: A failed recording followed by a move repeats at most that event

- **WHEN** recording the position past an event fails and the part of the stream holding it is
  reassigned before the recording is retried, to another instance or back to the same consumer after
  a rebalance or a lapse of its membership
- **THEN** at most that one event is delivered a second time
- **AND** no other event is delivered twice

#### Scenario: An undecodable entry does not block the group

- **WHEN** an entry that cannot be decoded is read
- **THEN** it is acknowledged, a warning names it, and the next event is delivered

#### Scenario: An unknown kind tag is skipped

- **WHEN** an entry whose kind tag the reading build does not know is read
- **THEN** it is acknowledged and skipped rather than raising or being redelivered

#### Scenario: One read skips a bounded number of bad entries

- **WHEN** more undecodable entries than the per-read bound precede an event
- **THEN** a read returns nothing once it has skipped that many
- **AND** a following read continues where it stopped and delivers the event

#### Scenario: A read with no timeout returns immediately

- **WHEN** the consumer is polled with no timeout against an empty stream
- **THEN** it returns nothing without waiting for events, within one bounded fetch plus the stated
  bounds of any other request it makes

#### Scenario: A read with a timeout is bounded by it

- **WHEN** the consumer is polled with a timeout against an empty stream
- **THEN** it returns nothing after approximately that timeout and not later

### Requirement: A consumer keeps its place while a delivery and its follow-ups run

A broker whose group membership lapses when a member stops reading SHALL be told how long the loop
may go between two reads before that happens. The composition root SHALL derive that interval from
the consolidation lease window, the longest a single delivery may legitimately run between two
renewals of the lease it holds. The consumer SHALL take the interval as a required constructor
argument and read no setting to obtain it. It SHALL NOT be left to a library default, which knows
nothing of the lease window and, at the default model read timeout, is shorter than one model call
plus its margin. A backend whose group has no membership session has nothing to lapse and takes no
such interval.

The loop delivers a handler's follow-ups itself without reading in between, so a chain of follow-ups
can outlast the interval even when no single delivery does. Exceeding it SHALL cost membership and
never position. The member's parts of the stream move to its peers, which resume at the recorded
positions, so an event already returned is not delivered again unless its own position had not been
recorded. That is at most one event per member. The member SHALL rejoin on its next read without a
restart.

The cost of a long interval SHALL be stated. The broker waits up to the same interval for a busy
member to rejoin a rebalance. While one instance is inside a long delivery, an instance starting or
stopping pauses the whole group's consumption for up to that interval.

#### Scenario: The interval follows the lease window

- **WHEN** a composition root builds a consumer whose group membership can lapse
- **THEN** the interval between reads it tolerates is derived from the consolidation lease window
- **AND** the consumer module reads no setting

#### Scenario: A delivery longer than the interval hands the work over without replaying it

- **WHEN** a delivery whose position was recorded outlasts the interval while a peer instance reads
- **THEN** the peer receives the events published afterwards on the parts of the stream that moved
- **AND** the event that was in hand is not delivered again

#### Scenario: A lapse after a failed recording repeats at most that event

- **WHEN** recording the position past an event failed and the delivery of that event then outlasts
  the interval
- **THEN** at most that one event is delivered again, by whichever member holds its part of the stream

#### Scenario: A member that lapsed rejoins on its next read

- **WHEN** the loop reads again after a delivery outlasted the interval
- **THEN** the consumer receives events again without the process restarting

### Requirement: One deployment-wide selector chooses the transport

Which transport this seam runs on SHALL be read from one selector shared by every seam of the
deployment, not from a switch of this seam's own. A deployment running one seam on a broker while
another stays in-process is a configuration nobody intends and one that is only discovered in
production, so it SHALL NOT be expressible.

The selector SHALL name a broker backend: the in-process transport, or one particular broker. It is a
choice among named backends, not a switch between in-process and "the broker". Every value the
settings schema admits SHALL either resolve every seam to that backend's transport or be refused at
composition, by a refusal that names the selector's setting and the value it holds. No value SHALL
fall through to the in-process transport where a seam is resolved from the selector: a resolver SHALL
compose the in-process transport only when the selector names it. The single-process runtime, which
composes the in-process transport directly and does not read the selector, is not such a place. The
rule SHALL hold at every place that resolves a seam from the selector, each on its own: a seam whose
resolver has no arm for a value SHALL refuse that value even when another resolver, or the backend
resolution itself, accepted it. A backend added for some seams and not yet for others is then a
refusal at start-up rather than a seam left quietly in process.

The selector's default SHALL be the in-process transport, so the single-process profile keeps
composing what it composes today with no configuration.

The selector SHALL be read by composition roots, through a module that resolves a backend from it. The
background memory package SHALL NOT read it, import that module, or read any setting: a transport that
reads settings is a transport that can disagree with the root that built it.

#### Scenario: The default is the in-process transport

- **WHEN** no broker selector is configured
- **THEN** the composition roots build the in-process transport

#### Scenario: The selector names a broker for every seam at once

- **WHEN** the selector names a particular broker
- **THEN** this seam resolves to that broker's transport, and there is no per-seam switch that
  could leave it in-process

#### Scenario: A value no seam can compose is refused, not composed in process

- **WHEN** the settings carry a selector value that no resolver has an arm for (settings built
  without validation, for example)
- **THEN** resolving the backend, the message gateway, the agent-event publisher and consumer, and the
  memory-event publisher and consumer each raises, naming the selector's setting and the value
- **AND** no in-process queue is built, no in-process agent-event pair is created, and the
  memory-event publisher registry still holds the no-op publisher

#### Scenario: A seam without an arm for a value refuses on its own

- **WHEN** the backend resolution returns a value that one seam's resolver has no arm for
- **THEN** that resolver raises, naming the selector's setting and the value, and composes nothing in
  process

#### Scenario: The package reads no setting

- **WHEN** the background memory package's modules are parsed
- **THEN** none reads the selector or imports the module that resolves it

### Requirement: The work that names a conversation rides the persisted-message signal, and adds no wire kind

The work that names a conversation SHALL reach the background memory tier on the message-persisted
signal that already crosses the seam, and SHALL NOT be given a wire kind of its own. The wire
registry (`background_memory/infra/events/codec.py`) SHALL be unchanged by it, and no new event
SHALL be published.

It SHALL NOT be folded into the consolidation **handler**. That handler's single-flight is a lease on
the ownership pair, deliberately wider than the chat-keyed rows it protects, and a trigger arriving
while the pair is leased is dropped with its thresholds unread
(`background_memory/handlers/consolidation.py`). Its thresholds are backlog-shaped for the
same reason they fit summarizing and not naming: a conversation worth titling is one message old and
therefore furthest from any threshold, so that fold would title only the conversations that grew long
enough not to need it.

What it joins instead is the **dispatch entry** for the persisted-message kind: the value a
composition root registers for that kind SHALL call the titling step and then the consolidation
handler, returning the consolidation handler's follow-up unchanged. A dispatch value holding two
steps is not a fold into either of them — the titling step inherits neither the lease nor the
thresholds, and neither step can drop the other's work.

The trigger SHALL therefore be a **state** rather than an event: the record's title origin, read on
every persisted message of that conversation. That is what makes the work self-healing across a
rolling deploy. A kind of its own would be published once, when the record is created, and an
instance whose build predates the kind acknowledges and skips it by design (`codec.py`) —
after which nothing would ever produce a second one, and the conversation would keep a provisional
title forever with no signal that anything was missed. A state is re-evaluated by the next message
that lands on an upgraded instance.

The cost of reading a state on every message SHALL be bounded by the read itself: one primary-key
lookup of the record, answering early for every message of every conversation that already has a
title and for every message of every surface that keeps no record. No model SHALL be called except
where that lookup says the title is still provisional.

Losing the work SHALL cost the conversation its generated title and nothing else, and SHALL be
recoverable: the provisional title the transport wrote renders and can be renamed, and the next
message on that conversation asks again. The number of model calls one conversation may cost SHALL
be capped, and the permission to make one SHALL be taken in the statement that records the attempt,
before the call, so a crash mid-call spends an attempt rather than looping.

#### Scenario: No wire kind is added

- **WHEN** the wire registry is compared before and after the change
- **THEN** it names the same set of tags
- **AND** no event is published for the titling work

#### Scenario: The titling step and the consolidation handler share one dispatch entry

- **WHEN** the dispatch table a composition root builds is inspected
- **THEN** the persisted-message kind maps to a value that calls the titling step and then the
  consolidation handler
- **AND** the follow-up it returns is the consolidation handler's, unchanged

#### Scenario: A leased ownership pair does not withhold a title

- **WHEN** a message is persisted for a conversation while a consolidation run holds the lease on its
  ownership pair
- **THEN** the titling step still runs and the title is still generated
- **AND** the run summarizing the other conversation is unaffected

#### Scenario: A message on a titled conversation costs one lookup

- **WHEN** a message is persisted for a conversation whose title is no longer provisional
- **THEN** the titling step returns after reading the record
- **AND** no model is called

#### Scenario: A conversation missed by an older instance is titled by its next message

- **WHEN** a message is consumed by an instance whose build predates the titling step, and a later
  message of the same conversation is consumed by an upgraded one
- **THEN** the conversation is titled on the later message
- **AND** nothing had to be re-published for that to happen

#### Scenario: The attempt is recorded before the model is called

- **WHEN** the process stops during the model call
- **THEN** the attempt has already been recorded
- **AND** a conversation cannot spend more than the cap however often it is redelivered

#### Scenario: The tier stores the title rather than answering the publisher

- **WHEN** the titling step completes
- **THEN** the generated title has been written to the store
- **AND** no event carrying it was published back through the seam

### Requirement: The package owns the policy it applies while its broker is unavailable

The background memory package SHALL declare its own policy for a broker that does not answer: when to
retry, how long to wait before the next attempt, and how often to report the outage. That policy
SHALL separate the decision from the waiting — it SHALL decide and return, and the caller SHALL await
the wait — so that a consumer applying it does not occupy the loop it runs on.

The package SHALL NOT use the shared synchronous policy object, which ends its decisions in a sleep
and is instantiated by several other components that are not changing. Duplicating that decision here
is accepted for the same reason the package's repositories duplicate statements: the alternative is
changing an object four other modules depend on.

The policy SHALL still report an ongoing outage at a bounded rate rather than on every attempt, so an
unreachable broker produces a periodic warning and not a flood.

#### Scenario: Waiting out an outage does not occupy the loop

- **WHEN** the broker does not answer and the consumer applies its outage policy
- **THEN** other work on the same event loop runs during the wait

#### Scenario: The policy decides without waiting

- **WHEN** the policy is asked whether to retry
- **THEN** it answers and returns, performing no sleep of its own

#### Scenario: An ongoing outage is reported at a bounded rate

- **WHEN** the broker stays unreachable across many attempts
- **THEN** the outage is reported periodically rather than once per attempt

#### Scenario: The shared synchronous policy is untouched

- **WHEN** the shared policy object and its instantiations outside this package are inspected
- **THEN** neither its behaviour nor its call sites changed

### Requirement: At most one delivery per ownership pair runs at a time

The loop SHALL run at most one delivery for any one `(owner_id, agent_id)` pair at a time. An event
whose pair is already being delivered SHALL wait for that delivery to finish rather than starting
beside it. Deliveries for different pairs SHALL proceed concurrently and SHALL NOT be serialized
against one another.

This is correctness, not throttling. The consolidation handler keys its in-flight run by that pair,
so two deliveries for one pair write to one run object: the continuation of one is resolved against
the other's run, and their pending message, offset and summary interleave. Their expected cursors are
equal, so the compare-and-swap that guards the commit cannot tell them apart and the committed
summary can describe a fragment of one delivery's work under a cursor that has passed both. The lease
already declares this boundary for the deployment; this is the same boundary inside one process,
where a lease acquirable by its own holder does not enforce it.

The rule SHALL NOT be stated as a global limit on concurrency, and SHALL NOT be implemented as one. A
deployment that raises its concurrency raises the number of pairs in flight, never the number of
deliveries per pair.

The wait SHALL be bounded or the queued event SHALL be dropped by a stated rule; an unbounded
accumulation of waiting deliveries for one busy pair is a queue nobody declared.

#### Scenario: A second event for one pair waits

- **WHEN** an event is polled for an ownership pair whose delivery is still running
- **THEN** it does not start while that delivery is running

#### Scenario: Two pairs run at once

- **WHEN** events for two different ownership pairs are polled
- **THEN** both deliveries run concurrently

#### Scenario: One pair's run object is never shared by two deliveries

- **WHEN** many events for one ownership pair are published in a burst
- **THEN** no two deliveries for that pair are active at the same time
- **AND** the run recorded for that pair is written by one delivery at a time

#### Scenario: The rule is per pair, not a global cap

- **WHEN** the serialization is inspected
- **THEN** it is keyed by the ownership pair
- **AND** raising the deployment's concurrency raises the number of pairs in flight

### Requirement: Stopping drains the deliveries in flight

A request to stop SHALL stop the loop from starting new deliveries and SHALL then wait, within a
stated bound, for the deliveries already running to finish before the loop returns.

A delivery may hold a lease on its ownership pair. A delivery abandoned at shutdown leaves that lease
held until it expires, which parks the pair for every instance of the deployment for the whole expiry
window — so an ordinary redeploy would park every pair that happened to be busy. Draining is what
makes a redeploy cost nothing.

Where a delivery does not finish within the bound, it SHALL be cancelled, and the cancellation SHALL
reach the handler as a cancellation rather than as a silent disappearance. The steps a handler takes
to release what it holds SHALL run to completion even though the delivery is being cancelled;
releasing a lease is itself an awaited operation, and a release that is cancelled at its first
suspension point releases nothing.

The composition root SHALL await the loop's own task on the way out rather than leaving it to be
collected, so that a process does not report itself stopped while its deliveries are still running.

#### Scenario: A stop waits for a running delivery

- **WHEN** the stop signal is set while a delivery is running
- **THEN** the loop starts no further delivery
- **AND** it returns only after that delivery has finished or its bound has passed

#### Scenario: A drained delivery leaves no lease behind

- **WHEN** the loop is stopped while a run holds a lease and the run ends on the stop
- **THEN** the lease is released before the loop returns
- **AND** an instance starting afterwards may take that pair without waiting for an expiry

#### Scenario: A cancelled delivery still releases what it holds

- **WHEN** a delivery is cancelled because it outlasted the drain bound
- **THEN** the release the handler performs runs to completion
- **AND** the pair is free rather than left leased until expiry

#### Scenario: The root awaits the loop

- **WHEN** the process stops
- **THEN** the composition root awaits the loop's task before it reports the process stopped

### Requirement: Liveness reports the oldest delivery, not the dispatcher

The loop's liveness signal SHALL report the age of the oldest delivery still in flight, and SHALL be
judged against the consolidation lease window — the same bound it is judged against today.

The bound was chosen when a delivery ran inside the loop, so a silent loop meant a stuck delivery. A
loop that only starts tasks would keep signalling briskly while every delivery it started was hung,
which is the one failure the signal exists to report. A dispatcher that is alive is not the question;
a tier whose deliveries are stuck is.

The signal SHALL be healthy when no delivery is in flight: an idle tier is not a stuck one.

#### Scenario: A stuck delivery is reported

- **WHEN** a delivery has been running longer than the lease window while the loop keeps polling
- **THEN** the liveness signal reports the tier as unhealthy

#### Scenario: An idle tier is healthy

- **WHEN** no delivery is in flight and the loop is polling
- **THEN** the liveness signal reports the tier as healthy

#### Scenario: The bound is still the lease window

- **WHEN** the liveness bound is compared with the consolidation lease window
- **THEN** it is derived from that window rather than stated independently of it

### Requirement: Concurrent deliveries widen the at-most-once loss window, and that width is stated

A broker-backed consumer records its position past an event before returning it, so an instance that
dies between the two loses that event. While deliveries ran one at a time that cost was one event per
instance. With deliveries running concurrently it is every delivery in flight on that instance.

That width SHALL be stated where the backend is described, in the same place the existing at-most-once
cost is stated, rather than left to be inferred from the concurrency. It SHALL be stated as the number
of deliveries in flight and not as a constant, because it follows the concurrency the deployment
composes.

The remedy SHALL NOT be to acknowledge after handling. That would require the consumer interface to
expose an acknowledgement, which it does not, and would make a failed handling a redelivery — which
for distillation means re-running work that writes memory, and for consolidation means a second run
against a cursor the first may already have moved.

What is lost SHALL be recoverable by the ordinary trigger path rather than by replay: a lost
consolidation trigger leaves the backlog untouched and the pair's next persisted message triggers it
again, and a lost titling signal leaves the title provisional until the conversation's next message.
A lost distillation event costs that task's memories and nothing else.

#### Scenario: The cost is stated as the deliveries in flight

- **WHEN** the document describing the broker-backed backend is read
- **THEN** it states that an instance that dies loses the events of every delivery in flight
- **AND** it does not state that figure as a constant

#### Scenario: The acknowledgement still precedes the delivery

- **WHEN** an event has been returned by a read
- **THEN** the group's recorded position is already past it
- **AND** the consumer interface still exposes no acknowledgement

#### Scenario: A lost trigger is re-triggered rather than replayed

- **WHEN** a consolidation trigger is lost with the instance that held it
- **THEN** the pair's backlog is unchanged
- **AND** the next persisted message for that pair triggers a run

### Requirement: No module of the package blocks the loop it runs on

No module of the background memory package SHALL perform a blocking call on the event loop: not a
synchronous database call, not a synchronous HTTP request, not a synchronous broker read, not a
sleep, and not a wait on a threading primitive.

Where the package needs something the rest of the system offers only synchronously, it SHALL reach it
through an interface of its own with an awaitable surface, and SHALL own the implementation behind
that interface. This is what the package's self-sufficiency is for: a seam the package declares can
be made asynchronous without asking a module that has other callers to change.

The one sanctioned exception is a body that must run off the loop because it is synchronous by nature
— a tool body — which SHALL be placed on an executor owned by its own call rather than run inline or
placed on a shared pool. That exception SHALL be named where it is used and SHALL NOT be generalised
into a way of calling synchronous code from this package.

Waiting on a stop signal SHALL NOT be a blocking wait. Reading whether it is set is not a wait, and
is permitted.

#### Scenario: The package performs no synchronous I/O on the loop

- **WHEN** the package's modules are parsed
- **THEN** none performs a synchronous database, HTTP or broker call from a coroutine
- **AND** none sleeps or waits on a threading primitive from one

#### Scenario: A synchronous dependency is reached through the package's own seam

- **WHEN** the package needs something available only synchronously elsewhere
- **THEN** it declares an awaitable interface of its own and owns the implementation behind it
- **AND** the module with other callers is not changed to serve it

#### Scenario: The tool-body exception is named where it is used

- **WHEN** a synchronous body is placed off the loop
- **THEN** it runs on an executor owned by that call
- **AND** the placement is the only one of its kind in the package

