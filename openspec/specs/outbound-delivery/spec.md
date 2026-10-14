# outbound-delivery Specification

## Purpose

How an agent response reaches the user: the supervisor publishes each message once to the outbox
with its source, and every transport reads its whole subscription and decides on its own side what
it can deliver. Progress and final messages share one channel, acknowledgement governs both queue
progress and release of resources staged for the message, and media travels by reference resolved at
delivery time.
## Requirements
### Requirement: Every published message names its source

An outbound message SHALL carry the source of the conversation it belongs to, and the publishing
boundary SHALL reject a message with no source rather than defaulting one. Delivery decisions SHALL
NOT depend on an implied default.

The source is a **delivery routing key**, not an identity axis. Its dictionary is the set of values
some transport claims, and that set SHALL NOT be assumed equal to the surface dictionary: a value may
route without naming a surface, and a surface may exist that no reader claims. The source SHALL
therefore be carried as its own field and SHALL NOT be derived from a scope's surface, nor a scope's
surface derived from it.

A message that names an undeliverable source SHALL remain detectable as such. Deriving the routing
key from an identity axis would make every undeliverable message name a real-looking surface and
silently retire that detection.

#### Scenario: A message with no source is refused at publish

- **WHEN** an outbound message is published without a source
- **THEN** publishing fails and the omission is visible

#### Scenario: Source survives to the reader

- **WHEN** a reader receives a published message
- **THEN** the source is the one the publisher set

#### Scenario: A routing key that no transport claims stays visible

- **WHEN** a message is published whose source no transport claims
- **THEN** the condition is detectable at the publishing boundary
- **AND** the source is not rewritten to a surface value that appears deliverable

### Requirement: A transport delivers only what it can deliver

A transport SHALL read everything on its subscription and SHALL decide for each message whether it
can deliver it. A message it cannot deliver SHALL be acknowledged and dropped. Every message a
transport reads SHALL be acknowledged, whether delivered or dropped.

A transport holding several connections SHALL decide per connection, not per instance. The decision
SHALL be made against the owner the message's scope names, the agent it names, and the chat, and the
message SHALL reach only connections whose own scope matches all three. A transport SHALL NOT deliver
a message to every connection it holds merely because it holds one the message belongs to.

The agent belongs in that match because a conversation id is unique within an owner **and an agent**,
and not across the agents of one owner. A client mints its own conversation ids and one owner may
hold several agents at once, so two conversations of one owner can carry the same id under different
agents; matching on the owner and the chat alone would hand one agent's answer to a connection
showing the other's, and it would do so silently — the frame was delivered, to a connection that
matched everything it was asked to match.

Those decisions SHALL also be independent in time: a decision about one connection SHALL NOT delay a
decision about another. A transport SHALL NOT hand a message to its connections in sequence such that
one connection's condition determines when the others are reached, because a subscription keeps at
most one message in flight and a reader loop settles one message before taking the next — so a delay
one connection imposes is a delay every owner on the instance pays.

The time one message can hold a subscription's in-flight slot SHALL be bounded, and the bound SHALL
NOT grow with the number of connections the transport holds. A per-connection deadline multiplied by
the roster is not a bound; it is the roster's size expressed in seconds.

#### Scenario: A browser transport drops a chat it has no client for

- **WHEN** a message arrives for a chat with no live browser client on this instance
- **THEN** the transport acknowledges it and delivers nothing

#### Scenario: A message reaches only the owner it belongs to

- **WHEN** a transport holds connections for two owners and a message arrives whose scope names one
  of them
- **THEN** only that owner's connections receive it
- **AND** the message is acknowledged once, as delivered

#### Scenario: A message reaches only the agent it belongs to

- **WHEN** a transport holds two connections of one owner, on two agents, whose conversation ids are
  the same string, and a message arrives whose scope names one of those agents
- **THEN** only the connection on that agent receives it
- **AND** the other connection receives nothing

#### Scenario: A dialogs transport delivers a chat it can reach

- **WHEN** a message arrives for a chat the dialogs provider can send to
- **THEN** the transport delivers it and acknowledges it

#### Scenario: A transport does not stall on a message it ignores

- **WHEN** a transport drops a message not addressed to it
- **THEN** subsequent messages continue to be delivered

#### Scenario: One connection that has stopped draining does not hold the others

- **WHEN** a message addressed to several connections is delivered and one of them never accepts its
  frame
- **THEN** every other connection receives the frame without waiting for that one
- **AND** the message is settled within the transport's per-message bound

#### Scenario: One owner's stalled connection does not silence another owner

- **WHEN** an instance holds a stalled connection for one owner and messages are published for a
  second owner
- **THEN** the second owner's messages are delivered, delayed by at most the per-message bound

#### Scenario: The per-message bound does not scale with the roster

- **WHEN** the transport's delivery path is inspected with many matching connections
- **THEN** the longest a single message can occupy the in-flight slot is the same as with one
  connection, apart from a fixed margin

### Requirement: Progress and final messages travel the same channel

Progress updates and final answers SHALL be published to the same outbox, distinguished by a flag on
the message. A transport that delivers only final answers SHALL acknowledge and drop progress
messages on its own side.

#### Scenario: A2A drops progress and delivers the final answer

- **WHEN** a task publishes progress updates followed by a final answer for an A2A chat
- **THEN** the A2A transport acknowledges each progress message without delivering it, and completes
  the waiting request with the final answer

#### Scenario: A browser client receives progress

- **WHEN** a task publishes a progress update for a browser chat with a live client
- **THEN** the client receives it

### Requirement: A request-response transport delivers on the instance holding the request

A transport whose delivery target is a connection held in this process SHALL keep a registry from
chat to the pending request and SHALL deliver only when the registry has an entry. A final message
with no matching entry SHALL be acknowledged and dropped.

#### Scenario: The answer returns to the instance that accepted the request

- **WHEN** a request accepted on one instance is answered by a task that ran on another
- **THEN** the instance holding the request delivers the answer and the other acknowledges and drops
  it

#### Scenario: An answer arriving after a timeout is dropped

- **WHEN** a final message arrives for a request that has already timed out
- **THEN** it is acknowledged and dropped, and nothing is delivered

#### Scenario: A timed-out request reports the timeout

- **WHEN** no final message arrives within the deadline
- **THEN** the request fails with a timeout and the task is marked failed

### Requirement: A shared-subscription transport delivers each message once across instances

A transport whose delivery works from any instance SHALL use a shared subscription, so exactly one
instance delivers a given message and no claim protocol is needed.

Progress SHALL survive a restart wherever the seam runs on a backend whose subscriptions can span
processes: a message read but not acknowledged before a crash SHALL be delivered again, to a peer if
one is reading. The instance that read it is gone and cannot retry, so redelivery is the only thing
that keeps an answer the agent already produced.

How the broker notices the crash, and how long that takes, belongs to the backend. It may be an idle
bound after which a peer reclaims the entry, or a reassignment once the broker observes that the
consumer departed. The duplicate this can cause is the same on every broker: a live reader too slow
for that bound has its message handed on, and the user receives it twice.

A backend that does not open shared subscriptions SHALL refuse such a transport's reader rather than
open it in another mode (see `transport-seam`), so the transport cannot start on that backend. It
SHALL NOT be run there as a broadcast subscription: every instance would then deliver every message.
Dialogs is the one shared-subscription transport, and the multi-user mode already refuses its ingress
(`multiuser-mode`). A deployment that needs it runs a backend that opens shared subscriptions.

Where the seam runs entirely inside one process, the crash this protects against takes every reader
with it and there is no peer to redeliver to. Such a backend MAY drop a subscription that has no
reader left, SHALL log what the drop discarded, and SHALL be the only kind of backend that does (see
`transport-seam`). A test pinning that drop SHALL name itself as covering the in-process backend, so
a backend that spans processes and honours the durability rule is not later read as failing a
regression.

#### Scenario: Two instances do not both answer one chat

- **WHEN** two instances run the dialogs transport and one message is published for a chat
- **THEN** the user receives it once

#### Scenario: An answer produced before a crash is not lost

- **WHEN** a message is read on one instance of a deployment whose backend spans processes, and that
  instance dies before acknowledgement
- **THEN** a surviving instance is delivered it again once the backend's reclaim or reassignment
  observes the crash

#### Scenario: A backend without shared subscriptions refuses the transport

- **WHEN** a shared-subscription transport opens its reader on a backend that declares it does not
  support shared subscriptions
- **THEN** opening the reader raises, naming the shared mode and the backend
- **AND** no broadcast subscription is opened in its place

#### Scenario: A single-process backend drops rather than hoards

- **WHEN** the last shared reader closes in a deployment whose backend is confined to one process,
  with messages still queued
- **THEN** the subscription is dropped and the drop is logged naming it and how many messages went
  with it
- **AND** reopening the name delivers no stale backlog

### Requirement: Media is delivered by resolving a reference

A transport delivering a photo or a file SHALL resolve the message's attachment reference through the
shared storage provider at delivery time. A transport whose channel cannot carry media SHALL
acknowledge and drop such a message.

Resolving a payload SHALL NOT run on the thread that holds the subscription's in-flight slot. A
blocking read of object storage cannot be given a deadline by the thread executing it, cannot observe
its transport's stop signal, and therefore can outlast the timeout that sizes the reader's shutdown
join — releasing the subscription under a reader still holding a message, which is then redelivered
behind the delivery that afterwards succeeds. The resolution SHALL instead run where the reader can
impose a deadline on it and abandon it.

Resolution SHALL be bounded in both time and concurrency: a resolution that exceeds its deadline SHALL
be abandoned, and the number of resolutions running at once SHALL be capped, because an abandoned
blocking read continues to completion and unbounded abandonment would starve the transport's other
work.

A payload that could not be resolved — a missing reference, an unavailable provider, an unreadable
blob, or an expired deadline — SHALL be acknowledged and dropped with the failure recorded, and SHALL
NOT be negatively acknowledged. A blob that did not arrive within the budget will not arrive faster on
a second attempt, and every attempt is charged to every owner waiting behind the slot.

#### Scenario: A photo is fetched at delivery

- **WHEN** a transport delivers a message carrying a photo reference
- **THEN** it fetches the bytes from the storage provider and sends them on its channel

#### Scenario: A channel without media drops it

- **WHEN** a photo message is addressed to a request-response chat
- **THEN** the transport acknowledges and drops it, and the request is unaffected

#### Scenario: A large attachment does not pause the other owners

- **WHEN** a message carrying a large attachment is delivered while messages for other owners are
  queued behind it
- **THEN** the fetch and encoding run off the thread holding the in-flight slot
- **AND** the message is settled within its resolution deadline whether or not the fetch has finished

#### Scenario: An unresolvable payload is dropped, not redelivered

- **WHEN** resolving a message's attachment fails or exceeds its deadline
- **THEN** the message is acknowledged and dropped and the failure is recorded
- **AND** it is not offered to the transport again

### Requirement: An outbound message carries identity and routing as separate fields

Every message the supervisor publishes on behalf of a task SHALL carry the `OwnerScope` of the work
that produced it, alongside — not instead of — its delivery source. The row it persists SHALL take
every ownership and conversation axis from that scope.

The internal event that carries an outbound message between the agent and the supervisor SHALL carry
the scope as a value rather than as axes flattened into separate keys. A boundary that takes a scope
apart and a boundary that reassembles one from configuration are the same defect seen from two sides.

The outbound persistence and delivery entry points SHALL take the scope as a required argument, so
that a caller with no scope is a compile-time or import-time absence rather than a value quietly
invented at the far end.

Those entry points SHALL NOT take a chat id beside the scope. The conversation a message is
delivered to and the conversation its stored row records are one fact, and two parameters carrying
one fact can name different chats: a caller holding a route from one place and an identity from
another will eventually pass both. The chat SHALL be read off the scope, and the conversion from the
scope's string axis to the value the transports route on SHALL happen in exactly one place.

What is single is the rule, not the type it yields and not the number of times it is called. That
rule SHALL have a stated answer for an axis no numeric reading applies to, rather than reaching one
by falling through an exception handler: only the dialogs and agent-to-agent routes need an integer,
while a browser conversation is whatever non-empty string its own boundary admitted.

The rule SHALL narrow the axis **only where the narrowing loses nothing** — where the string is the
canonical decimal spelling of the integer it reads as — and SHALL carry every other spelling through
unchanged. Narrowing wherever a numeric reading merely *exists* is not the same rule and SHALL NOT
be used: `"01"` reads as `1`, and a caller that recorded `1` beside a row whose chat column held
`"01"` would have written one conversation under two names. The rule SHALL NOT refuse the message,
invent a number, or normalize the axis into one — a transport that cannot route what it is handed
decides that on its own side, where dropping it costs one message rather than rewriting which
conversation the row belongs to.

Every site that carries the axis beside the scope SHALL apply that one rule rather than re-deriving
it. A second `int()` anywhere on the path is the defect the rule exists to prevent, one hop further
out: the values a transport carries beside the scope reach the same stored message identity, so a
site that narrows differently writes the same message under a second identity and cannot look its
own row up again. A reader that only needs to *route* SHALL decide on its own side and SHALL NOT
treat a narrowed zero as an absent chat.

#### Scenario: A task-addressed message inherits its producer's scope

- **WHEN** the agent emits a progress line, a typing signal, or a final answer
- **THEN** the event carries the run's scope
- **AND** the supervisor persists and publishes under that scope without minting one

#### Scenario: The outbound door requires a scope

- **WHEN** the outbound send and persist entry points are inspected
- **THEN** each takes a required scope argument
- **AND** none constructs one from a source and a chat id

#### Scenario: Identity and routing do not overwrite each other

- **WHEN** a message is published whose delivery source and whose scope surface differ
- **THEN** the stored row records the scope's surface and the published message keeps its own source

#### Scenario: The door names the conversation once

- **WHEN** the outbound send and persist entry points are inspected
- **THEN** none takes a chat id beside its scope
- **AND** the chat a message is delivered to is the chat its stored row records

#### Scenario: A conversation no numeric reading applies to passes the door unchanged

- **WHEN** a message is published for a browser conversation whose chat axis is not a number
- **THEN** the stored row records that axis exactly as the scope carries it
- **AND** the single conversion returns it unchanged rather than refusing the message or replacing
  the axis with a number

### Requirement: The published payload carries the producing scope

The payload that crosses the seam SHALL carry the `OwnerScope` of the work that produced it, as a
required field beside its delivery source. The inbound envelope already carries a scope across the
same seam; the outbound payload SHALL do so too, so that identity crosses the boundary in both
directions rather than being reconstructed on one side.

The scope SHALL be read off the producing work and SHALL NOT be minted, defaulted, or derived from a
routing field at the publishing boundary. A publisher with no scope SHALL be an absent argument, not
a value invented at the far end.

The payload SHALL NOT carry a chat id beside its scope. Two fields carrying one fact can name
different chats, and a caller holding a route from one place and an identity from another will
eventually pass both. The chat a reader routes on SHALL be derived from the scope through the single
conversion above, which SHALL serve both directions of the seam and SHALL NOT be duplicated per
direction or per reader.

A reader SHALL NOT assume that conversion yields an integer. The scope's axis is a string on every
surface, and only the readers that register their delivery targets by number require one; a reader
that does SHALL check for itself and settle what it cannot route on its own side, rather than the
conversion being widened to guarantee a type for it. Guaranteeing one would put the coercion back at
the boundary that serves every surface, which is where coercing a browser conversation's opaque id
silently re-points it at another conversation.

The delivery source SHALL remain its own field and SHALL NOT be derived from the scope's surface: the
source is a routing key whose dictionary is the set of values some transport claims, and a message
naming an undeliverable source SHALL stay detectable as such.

#### Scenario: The scope survives to the reader

- **WHEN** a reader receives a published message
- **THEN** its scope is the one the producing work carried, with all four axes intact
- **AND** the delivery source is the one the publisher set, unchanged by the scope's surface

#### Scenario: The payload names the conversation once

- **WHEN** the published payload type is inspected
- **THEN** it carries no chat id field of its own beside the scope
- **AND** the chat a reader routes on is derived from the scope through the single conversion

#### Scenario: A spelling that is not canonical is carried through, not folded

- **WHEN** a message is published whose chat axis reads as a number but is not that number's
  canonical decimal spelling
- **THEN** the conversion returns the axis unchanged
- **AND** every site that carries the axis beside the scope records the same spelling, so the
  message is stored once and can be found again by the id it was stored under

#### Scenario: A conversation whose id narrows to zero still receives its messages

- **WHEN** a message is published for a conversation whose chat axis is `0`
- **THEN** the reader holding that conversation's connection is selected
- **AND** a narrowed zero is not read as an absent chat

#### Scenario: A reader that needs a number decides on its own side

- **WHEN** a payload whose scope names a chat no numeric reading applies to reaches a reader that can
  only route numbers
- **THEN** that reader acknowledges and drops it, as it does for any message it cannot deliver
- **AND** the conversion neither refuses the payload nor rewrites the axis to make it routable

#### Scenario: A chat collision across owners stays two conversations

- **WHEN** two owners each hold a conversation with the same chat id and a message is published for
  one of them
- **THEN** the payload names that owner through its scope
- **AND** the message is distinguishable from one published for the other

### Requirement: A producer with no author publishes a visibly synthetic scope, and may be refused

A producer that acts on no user's behalf — an extension speaking through the plugin API — SHALL NOT
invent a user. It SHALL publish under a scope whose ownership pair is the instance's configuration,
which names no real owner once owners are resolved per person, and which SHALL be recognisable as
synthetic at the point it is built.

Where identities are resolved per person, such a message can reach no one: its owner matches no
connection. The publishing boundary SHALL refuse it at the door rather than publishing a message no
reader can consume. The refusal SHALL name the limitation, so an extension author sees an unsupported
capability rather than a message that vanishes.

#### Scenario: An extension message is refused while identities are resolved per person

- **WHEN** an extension sends a message through the plugin API on an instance resolving owners per
  person
- **THEN** the send fails with a stated reason naming the unsupported capability
- **AND** nothing is published and nothing is written to any owner's history

#### Scenario: An extension message is unchanged while the instance has one owner

- **WHEN** an extension sends a message on an instance whose owner is its configured constant
- **THEN** the message is persisted and published exactly as before

### Requirement: Every outbound message is published once, through the publishing boundary

Every message bound for a user — final answer, progress update, typing action, photo, file — SHALL be
published to the outbox exactly once, through the publishing boundary and never around it. No
producer SHALL call a transport, a delivery callback or a per-transport sender, and none SHALL hold a
registry of which transport serves which chat.

The producer MAY be the supervisor or a run; the boundary is the same object for both, and the rule
is stated about the boundary rather than about the supervisor because there is now more than one
producer.

A transport is the one producer that publishes without going through that boundary, and only for
content it produced itself and cannot deliver alone — the echo of a user's own line, whose other
recipients may be connections another instance holds. The boundary exists to persist what belongs in
history and to mirror the message onto the observability bus; a transport has already written the
row the echo repeats, and the echo is not agent output to be mirrored, so routing it through the
boundary would duplicate the first and falsify the second. It publishes one message, exactly once,
and reports the failure rather than swallowing it, which is what the rule is actually for.

What makes this safe is that the boundary delivers nothing itself: it persists what belongs
in history, publishes one message to the outbox and returns acceptance, so a producer's report of
success means the message was taken, never that a user received it.

The acceptance it returns SHALL be true. The boundary MAY catch a failed publish rather than letting
it end the turn that produced it — an undelivered reply must not take down the run that wrote it —
but it SHALL then report failure to its caller, in the same shape it reports success, and SHALL NOT
report success for a message the outbox refused. Reporting a message as sent when publishing raised
is worse than raising: the producer records an answer no transport will ever read, and the only trace
is a log line nobody is watching. A boundary that catches SHALL log the failure at error level,
because once publishing is all-or-nothing a failed publish is not a degraded delivery but a message
that reached no one.

One declared deviation qualifies that last clause. On a broker whose append cannot be cancelled, a
publish that raised because a bounded wait expired may still land afterwards (`transport-seam`,
*Publishing is all-or-nothing and reports failure by raising*). The boundary cannot know which case
it is in, so it SHALL still report the failure, log it at error level and emit no observability
event for it. A caller that retries from its own record may then deliver the message twice. That
duplicate is the declared cost, and the boundary SHALL NOT try to hide it by reporting success for a
publish that raised.

A message the boundary failed to publish SHALL NOT be mirrored onto the observability bus as an
outbound event. Those events are read as evidence that a message was sent; emitting one for a message
that was refused repeats the same false report at a second door, where nothing is left to contradict
it.

What the boundary already persisted SHALL NOT be rolled back when the publish fails. The row is what
the next turn's context reads, and deleting it would make the producer contradict on a retry
something it has already said. The failure belongs in the returned result and in the log.

#### Scenario: One publish reaches whichever transport is concerned

- **WHEN** any producer answers a chat
- **THEN** it publishes once and performs no per-transport dispatch

#### Scenario: No delivery callbacks remain

- **WHEN** producers are inspected for response subscriptions, browser sinks or transport fan-out
  wrappers
- **THEN** none is found

#### Scenario: A run's publish is acceptance, not delivery

- **WHEN** a run publishes a photo or a file and the call reports success
- **THEN** the message is on the outbox
- **AND** nothing has yet been sent on any transport's channel

#### Scenario: A refused text publish is reported as a failure

- **WHEN** the outbox raises while the boundary is publishing a text message
- **THEN** the boundary reports failure to its caller with a stated reason, rather than success
- **AND** the failure is logged at error level

#### Scenario: A publish whose wait expired is reported as failed even if it lands later

- **WHEN** the outbox raises because its bounded wait for the broker expired, on a backend that
  declares an uncancellable append, and the broker then accepts the message
- **THEN** the boundary has already reported failure to its caller and logged it at error level
- **AND** no outbound observability event was published for it

#### Scenario: A refused publish emits no outbound observability event

- **WHEN** the outbox raises while the boundary is publishing a text message or a typing action
- **THEN** no outbound or typing event is published on the observability bus for it

#### Scenario: A refused typing action reports failure

- **WHEN** the outbox raises while the boundary is publishing a typing action
- **THEN** the boundary reports failure rather than success

#### Scenario: A refused publish leaves history as it was

- **WHEN** the outbox raises for a message whose history row was already written
- **THEN** the row remains, and the next turn's context still contains it

#### Scenario: A transport's own echo is published, not delivered process-locally

- **WHEN** a transport produces content whose recipients may include connections another instance
  holds
- **THEN** it publishes one message to the outbox rather than sending to the sockets it happens to
  hold
- **AND** a publish that fails is reported to the sender rather than leaving a line that is stored
  and never shown

### Requirement: A run reaches the outbox through a boundary it was given

A run SHALL reach the outbound boundary through an object handed to it by the composition root that
built the run. It SHALL NOT hold individual sender callables captured from another component, and it
SHALL NOT resolve the boundary from a process-local registry.

Loose callables are the failure this replaces: two functions taken off another component's bridge and
carried inside the run as a dictionary is a live reference into another process's object graph, which
is exactly what cannot cross the day the run moves. Resolving from a registry instead does not fix
it — it relocates the same dependency behind a global and turns a missing composition into an error
at call time rather than at construction.

Constructing the boundary inside the run SHALL NOT be treated as an alternative: it needs the outbox,
and a run that obtains an outbox without being given one has obtained it from the registry by another
name.

What changes when the run moves to its own process SHALL be the outbox implementation behind the
boundary, and nothing in the run, the tools or the boundary itself.

#### Scenario: A run holds one outbound boundary, not senders

- **WHEN** a run's tool context is inspected
- **THEN** it holds the outbound boundary it was given
- **AND** it holds no dictionary of sender callables

#### Scenario: The boundary is not resolved from a registry

- **WHEN** the run's path to the outbox is inspected
- **THEN** no call reads a process-local runtime registry to find it

#### Scenario: An unwired run reports transport unavailable rather than raising

- **WHEN** a run is constructed without an outbound boundary and a tool tries to send a file
- **THEN** the tool reports that the attachment transport is unavailable
- **AND** the run continues

### Requirement: A connection-bound transport is never handed the messages of the process it replaced

A transport whose delivery target is a connection held in this process — a browser WebSocket, a
pending a2a request — SHALL start each process owed nothing. Messages published while an earlier
process held the same subscription name SHALL NOT be delivered to the new one, and the new process
SHALL acknowledge nothing on behalf of the one it replaced.

Those messages were addressed to connections that died with that process. A client reconnecting to
the restarted instance matches on owner and chat, not on the connection that is gone, so a replayed
backlog would land one conversation's stale answers in a live session — after the fact, out of
order, and indistinguishable from a fresh reply.

#### Scenario: A reconnecting client is not replayed the old process's answers

- **WHEN** an instance holding browser connections dies with messages published for one of its
  chats, then restarts, and the client reconnects to it
- **THEN** the client receives only messages published after the new subscription opened

#### Scenario: A request that died with its instance is not answered by the replacement

- **WHEN** an a2a request accepted by an instance that then died is answered after that instance
  restarts
- **THEN** the restarted instance holds no pending entry for it, acknowledges the message and
  delivers nothing

### Requirement: An unresponsive connection is dropped rather than retried

A transport SHALL give each connection its own deadline for accepting a frame, covering both waiting
for its turn on that connection and the send itself. A connection that misses its deadline SHALL be
removed from the transport's roster and its channel closed, and the message SHALL NOT be redelivered
on its account.

A connection that is not draining is not a transient condition, and redelivery offers the same message
to the same roster containing the same connection: the attempts produce one identical outcome and
multiply the silence for every other owner. Removing the connection immediately, rather than leaving
it to whatever cleanup its own handler eventually performs, is the point — otherwise the next message
pays the same deadline again.

Nothing durable is lost by the drop: the message is in the owner's history, and a client that
reconnects reads it from there.

#### Scenario: A stalled connection is closed and removed

- **WHEN** a connection does not accept its frame within its deadline
- **THEN** the transport closes it and removes it from the roster
- **AND** the next message for that owner is not delayed by it

#### Scenario: A stalled connection does not trigger redelivery

- **WHEN** the only matching connection for a message is dropped for missing its deadline
- **THEN** the message is acknowledged rather than negatively acknowledged

#### Scenario: A dropped client recovers its messages on reconnect

- **WHEN** a client that was dropped mid-delivery reconnects
- **THEN** it reads the messages it missed from the owner's history

### Requirement: Concurrent fan-out preserves order within a conversation

Delivering to connections concurrently SHALL NOT reorder a conversation. Frames for one conversation
SHALL leave each connection in the order they were published, so a progress line published before a
final answer arrives before it.

Two properties SHALL hold together. Concurrency SHALL be across connections only, never across the
messages of one subscription: the reader settles a message before taking the next, so at most one
outbound frame per subscription is ever in flight. And sends to a single connection SHALL NOT overlap,
whatever produced them — an outbound delivery, an inbound echo, or a keepalive all reach the same
channel from different tasks, and interleaving two writes on one channel corrupts the frame stream
rather than merely reordering it.

A transport that buffers frames per connection SHALL keep that buffer first-in-first-out and SHALL
treat a full buffer as an unresponsive connection, dropping it under the rule above. It SHALL NOT
discard an individual frame from the middle of a conversation, which would leave the client showing an
answer whose progress never arrived.

#### Scenario: Progress precedes the final answer on every connection

- **WHEN** progress updates and then a final answer are published for a chat with several live
  connections
- **THEN** each connection receives them in that order

#### Scenario: Two frames never interleave on one channel

- **WHEN** an outbound delivery and an echo or keepalive frame target the same connection at once
- **THEN** one completes before the other begins on that connection

#### Scenario: A full per-connection buffer drops the connection, not a frame

- **WHEN** a transport buffers per connection and one connection's buffer fills
- **THEN** that connection is dropped as unresponsive
- **AND** no frame is removed from the middle of another connection's sequence

### Requirement: Redelivery is reserved for a delivery that was never attempted

A transport SHALL negatively acknowledge a message only when the delivery was not carried out at all —
the runtime that performs sends did not run it. Any delivery that ran SHALL be acknowledged, whether
it reached a connection, found none left, or could not build its payload.

This preserves the reason the transport waits for its delivery instead of scheduling it and moving on:
a stopped-but-not-closed event loop accepts the coroutine and never runs it, so scheduling by itself
says nothing about delivery, and the acknowledgement has to rest on an outcome the transport observed.
It also narrows the ladder to the only condition a retry can resolve. A transport SHALL therefore be
able to tell a delivery that never started from one that started and was slow, rather than reading a
single timeout as both.

The number of redelivery attempts for one message SHALL remain bounded, and exhausting them SHALL
acknowledge the message with the failure recorded, because a subscription that never recovers would
otherwise hold its only in-flight slot against every message behind it.

#### Scenario: A delivery that never ran is redelivered

- **WHEN** the transport's runtime accepts the delivery but does not run it before the deadline
- **THEN** the message is negatively acknowledged and offered again, within the bounded attempt limit

#### Scenario: A delivery that ran and reached nobody is acknowledged

- **WHEN** a delivery runs and every matching connection has failed or been dropped
- **THEN** the message is acknowledged and dropped rather than redelivered

#### Scenario: A slow delivery is not mistaken for one that never started

- **WHEN** a delivery starts and exceeds the transport's per-message bound
- **THEN** it is acknowledged with the condition recorded as an error
- **AND** it is not counted as a failed handover

#### Scenario: An exhausted attempt ladder releases the slot

- **WHEN** a message reaches the attempt limit without being handed over
- **THEN** it is acknowledged and dropped and the messages behind it are delivered

### Requirement: The echo of a user's own message reaches every connection of that conversation

When a user sends a message on a connection, the message SHALL be echoed back to **every** connection
of that owner's conversation, wherever that connection is held, and to no connection outside it. The
sending connection is one of them and SHALL NOT be treated specially.

The echo is what makes a conversation shared rather than per-socket. A second tab that never renders
the user's line then renders the agent's answer to it, and the transcript reads as though the agent
answered nothing. Assistant output does not have this problem — it is published once and read back by
every instance, so it reaches whichever one holds the socket — and the echo SHALL hold the same
property, because a user with two views of one conversation cannot tell which of the two paths a line
travelled.

Selecting the recipients from the connections one process happens to hold SHALL satisfy this
requirement only where the deployment guarantees that every connection of an owner is held by a single
process. Where no such guarantee exists, the transport SHALL provide the reach itself rather than
relying on it. The requirement is on the reach; the mechanism is a separate decision and either
mechanism SHALL be judged by this property.

The echo SHALL obey this rule on every path that produces one, including the replay of a message whose
stored row is already marked processed. A replayed echo SHALL NOT be narrowed to the connection that
resent it: the connection that resends is not necessarily the connection that is missing the line.

#### Scenario: Two connections of one owner in different processes both receive the echo

- **WHEN** an owner holds two connections to the same conversation on two different transport
  instances and sends a message on one of them
- **THEN** both connections receive the echo of that message
- **AND** the echo carries the same content, timestamp and client message id on both

#### Scenario: The sending connection receives its own echo

- **WHEN** a user sends a message
- **THEN** the connection it was sent on receives the echo like any other connection of that
  conversation

#### Scenario: The echo does not reach another owner

- **WHEN** a transport instance holds connections for two owners and one of them sends a message
- **THEN** only the sending owner's connections for that conversation receive the echo
- **AND** the other owner's connections receive nothing

#### Scenario: A replayed echo reaches the same connections as the first

- **WHEN** a client resends a message whose stored row is already marked processed, and another
  connection of the same conversation is held elsewhere
- **THEN** the replayed echo reaches every connection of that conversation, not only the resending one

### Requirement: The echo carries the facts only the server holds

The echoed message SHALL carry the timestamp of the persisted row, the bound attachment records in
their public form, and the `client_message_id` the sender minted. Every path that produces an echo
SHALL read the timestamp from the stored record, so that two echoes of one message cannot name two
different times.

A client MAY render its own message optimistically to hide the round trip, since it holds the text and
its own client message id. That rendering SHALL NOT be treated as satisfying the previous requirement.
It reaches only the connection that sent the message, which is not the connection the defect concerns;
and it can draw neither the time the row was stored — which is what orders the line against the rest of
the history — nor the attachment records, which exist only after binding and which the client holds
only as opaque ids.

The `client_message_id` on the echo is what lets a connection recognise a line it has already
rendered, whether it rendered it optimistically or from an earlier echo. Without it, broadening the
echo's reach would turn a retry into a duplicate line.

#### Scenario: The echo names the time the row was stored

- **WHEN** a message is persisted and echoed
- **THEN** the echo's timestamp is the one the stored row carries
- **AND** an echo replayed later for the same message names that same timestamp

#### Scenario: An echo with attachments carries the bound records

- **WHEN** a user sends a message with attachments and the attachments are bound to it
- **THEN** the echo carries the public form of the bound records, not the ids the client submitted

#### Scenario: An optimistically rendered line is reconciled, not duplicated

- **WHEN** a client renders its own message before the echo arrives and the echo then arrives
- **THEN** the echo carries the client message id the sender minted
- **AND** the connection can identify the echo as the line it already rendered
