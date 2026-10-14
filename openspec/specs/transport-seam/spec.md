# transport-seam Specification

## Purpose

The interfaces and message shapes that separate transports from the supervisor: an inbox and an
outbox composed into one gateway, a named reader with a subscription mode and ack/nack, the
transport lifecycle surface, and the packaging rule that the `transport/` package imports nothing
else from the repo. Both sides depend on this seam and neither owns the backend, which lives behind
it in `transport/infra/` and is named only by composition roots.
## Requirements
### Requirement: Transports depend on the seam and nothing else in the repo

The `transport/` package SHALL NOT import `supervisor` or `agent`, at module level or lazily inside
functions. A transport SHALL import `transport.interfaces` for everything it needs from the core, and
MAY import shared infrastructure that belongs to neither side (chat storage, blob storage,
observability). The supervisor's runtime modules SHALL depend on `transport.interfaces` alone and
SHALL NOT import a backend module: the gateway SHALL be a required constructor argument rather than
a default they build for themselves.

Naming a backend SHALL be confined to the composition boundary: a production entry point, the test
bootstrap, or a composition module those entry points call to resolve a backend from configuration.
Such a module is part of the boundary rather than an exception to it, and the `transport/` package
SHALL NOT import it — a transport that could reach the resolver could reach a backend, which is the
dependency this requirement exists to prevent. More than one backend MAY exist behind the seam at once;
which one a process runs SHALL be a configuration decision taken at that boundary and nowhere else.

#### Scenario: No transport reaches into the core

- **WHEN** every module under `transport/` is inspected for imports, including function-local ones
- **THEN** none names `supervisor` or `agent`

#### Scenario: The backend is behind the seam rather than owned by either side

- **WHEN** the gateway backend is inspected
- **THEN** it lives in `transport/infra/`, subclasses the seam's interfaces, and is passed by the
  composition root to the transports and to the supervisor loop alike

#### Scenario: The supervisor names the seam and never a backend

- **WHEN** the imports of `supervisor/runtime.py`, `supervisor/bootstrap.py` and
  `supervisor/runtime_context.py` are inspected
- **THEN** none names a module under `transport/infra/`, and a runtime cannot be constructed without
  being handed a gateway, so replacing the backend leaves them untouched

#### Scenario: The backend resolver sits at the boundary, not inside the transports

- **WHEN** the module that resolves a backend from configuration is inspected
- **THEN** its importers are composition roots only
- **AND** no module under `transport/` imports it, at module level or lazily

### Requirement: The gateway composes two independent directions

`IMessageGateway` SHALL expose an `inbox` and an `outbox` as separate interfaces, sharing no state:
neither direction's progress SHALL depend on the other being read. Each direction SHALL carry both of
its halves on its own interface — `IInbox.submit` and `IInbox.poll`, `IOutbox.publish` and
`IOutbox.reader`. A transport SHALL write through `inbox` and read through `outbox`, and the
supervisor the other way around; which half a holder may call is a rule about the holder, not a
property of the method set, exactly as it already is for `IOutbox.publish`.

A transport MAY also publish to the outbox, for content whose recipients are not necessarily the
connections its own process holds — the browser's echo of a user's own line. That is a producer role
rather than a second read path: the transport still reads only its own subscription, and it SHALL NOT
publish anything it could deliver from the sockets it holds, because such a message is one every
other reader receives and drops for no gain.

`IInbox` SHALL keep exactly those two methods. It SHALL NOT gain `ack`, `nack`, a receipt, a handle,
or any other settlement operation, because settling an inbound message is `poll`'s own second job
(see *A poll settles the envelope the previous poll returned*). The outbound half needs receipts
because a subscription outlives its readers and two equal payloads must stay distinguishable; inbound
has at most one unsettled message per inbox, so a receipt would name something that was never
ambiguous while adding a map to every backend and a second way for a consumer to be wrong.

#### Scenario: A transport is given the composition, not the parts

- **WHEN** a transport is constructed
- **THEN** it receives one `IMessageGateway` and reaches both directions through it

#### Scenario: The directions are independently substitutable

- **WHEN** a test supplies an in-process fake for one direction
- **THEN** the other direction can be left unimplemented without the transport failing to construct

#### Scenario: Inbound is consumed without acknowledgement

- **WHEN** `IInbox.poll` returns a message
- **THEN** the message is gone from the queue and nothing acknowledges it; durability for inbound is
  the transport's unprocessed record (see `inbound-submission`), not a redelivery guarantee from the
  queue

#### Scenario: Inbound settles without an acknowledgement method

- **WHEN** `IInbox` is inspected
- **THEN** it declares `submit` and `poll` and nothing else, and no member of it names an
  acknowledgement, a receipt, or a delivery handle
- **AND** a polled message is settled by the consumer's next `poll`

#### Scenario: A transport publishes only what it cannot deliver itself

- **WHEN** a transport publishes to the outbox
- **THEN** the message is one whose recipients may be held by another instance
- **AND** every other reader acknowledges and drops it by its delivery source, as it does for any
  message that is not its business

### Requirement: Submission is durable-or-raise

`IInbox.submit` SHALL return `None` when the message has been accepted for delivery, and SHALL raise
when it has not. It SHALL NOT return a result describing what the message became — intent, task id,
or slash command — because that is decided elsewhere and, in a multi-instance deployment, on another
machine.

Unlike `IOutbox.publish`, `submit` takes no declared deviation, so a backend's own wait for the
broker's answer SHALL NOT be shorter than the point at which its client gives the record up. A wait
that expires first raises while the client is still retrying, which reports as not accepted a message
that lands — and the caller's answer to a rejection is to leave its journal row unprocessed and submit
it again, so the owner's line is handed over twice.

#### Scenario: Acceptance is signalled by returning

- **WHEN** `submit` returns without raising
- **THEN** the caller treats the message as handed over and marks its own record processed

#### Scenario: Rejection is signalled by raising

- **WHEN** the underlying queue is unavailable
- **THEN** `submit` raises and the caller's record stays unprocessed for a later retry

#### Scenario: The submission wait outlasts the client's own give-up

- **WHEN** a backend's wait for the broker's answer is compared with the point its client stops retrying
- **THEN** the wait is not the shorter of the two

### Requirement: A reader declares a name and a subscription mode

`IOutbox.reader` SHALL take the transport's stable name and a subscription mode, and SHALL return an
`IOutboxReader`. `SHARED` SHALL mean competing consumers: exactly one reader across all instances
receives a given message. `BROADCAST` SHALL mean one independent subscription per instance: every
instance receives every message, and a reader SHALL begin at the present moment rather than
replaying what it missed. That present SHALL be fixed by the time `reader` returns, not at the
reader's first poll. A transport opens its reader before its reader thread starts polling, and a
message published in between belongs to that reader. The implementation SHALL derive the consumer
identity from the name and, for `BROADCAST`, the instance; the instance SHALL NOT appear in
transport code.

A backend MAY support only `BROADCAST`. That is a capability the backend declares; callers do not
discover it by trial. Opening a mode the backend does not support SHALL raise `ValueError` naming
the mode and the backend. The refusal SHALL come before anything is created, so it leaves no
subscription and no claim behind. It SHALL NOT fall back to the mode the backend does support: a
`SHARED` transport opened as `BROADCAST` delivers every message once per instance, which is the
duplicate `SHARED` exists to prevent. The Kafka backend (`transport/infra/kafka_gateway.py`) is such
a backend.

Unacknowledged `SHARED` progress SHALL survive a restart on any backend that opens `SHARED` and whose
subscriptions can span processes. That is the guarantee the mode exists for: a message read by an
instance that then died is one no peer can otherwise know about, and dropping it loses an answer
already produced. A backend that does not open `SHARED` holds no `SHARED` progress, so this
guarantee does not bind it.

A backend that cannot span processes MAY deviate. In one process there is no second consumer, so a
subscription whose last reader closed is a write-only sink, and replaying a full backlog at the next
open is worse than a drop that says so. Such a backend SHALL state the deviation where it implements
it and SHALL log what the drop discarded, naming the subscription and the number of messages. The
deviation is a property of that backend, not a licence in the seam: a backend whose subscriptions
can span processes SHALL NOT take it.

#### Scenario: A shared reader resumes after a restart

- **WHEN** a `SHARED` reader on a backend that opens `SHARED` and whose subscriptions can span
  processes is closed with a message read but unacknowledged, and a new reader opens with the same
  name
- **THEN** that message is delivered again

#### Scenario: A broadcast reader starts at the present

- **WHEN** a `BROADCAST` reader opens after messages have already been published
- **THEN** it receives only messages published from that point on

#### Scenario: A broadcast reader starts when reader returns, not at its first poll

- **WHEN** `reader` returns a `BROADCAST` reader and a message is published before that reader's
  first `poll`
- **THEN** the first `poll` delivers that message

#### Scenario: Every instance sees a broadcast message

- **WHEN** two `BROADCAST` readers with the same name are open
- **THEN** both receive the same published message

#### Scenario: Exactly one shared reader sees a message

- **WHEN** two `SHARED` readers with the same name are open
- **THEN** a published message is delivered to one of them, not both

#### Scenario: A backend that supports only broadcast refuses a shared reader

- **WHEN** `reader` is called with `SHARED` on a backend that declares it does not support shared
  subscriptions
- **THEN** it raises `ValueError` naming the `SHARED` mode and the backend
- **AND** no subscription and no claim exist afterwards, so a `BROADCAST` reader opened under the
  same name receives the next published message exactly once

#### Scenario: A single-process backend says what its drop discarded

- **WHEN** the last `SHARED` reader of a backend confined to one process closes with messages still
  queued
- **THEN** the subscription and its backlog are dropped
- **AND** the drop is logged naming the subscription and how many messages went with it
- **AND** reopening the name delivers no stale backlog

### Requirement: The queue offers no filters

Neither `IOutbox` nor `IOutboxReader` SHALL accept a filter, a predicate, or a selector narrowing
what a reader receives. A reader SHALL receive everything on its subscription and SHALL decide on its
own side what is its business.

#### Scenario: Nothing narrows the stream

- **WHEN** the outbox interfaces are inspected
- **THEN** no method takes a chat id, a source, a message kind, or a progress flag as a selector

### Requirement: Acknowledgement confirms handling and releases resources

A message that a reader has finished with SHALL be acknowledged, including a message the reader
decided was not its business — that case SHALL be `ack` followed by a drop, and SHALL NOT be `nack`,
because an unacknowledged message holds its position and stalls the messages behind it. `nack` SHALL
mean an attempted delivery failed transiently and the message should be delivered again.
Acknowledgement SHALL also be the point at which resources tied to the message are released, such as
temporary files staged for it.

#### Scenario: A message that is not this transport's business is acknowledged

- **WHEN** a reader receives a message addressed to a chat it cannot deliver to
- **THEN** it acknowledges the message and delivers nothing

#### Scenario: An unacknowledged message stalls its successors

- **WHEN** a reader neither acknowledges nor negatively acknowledges a message
- **THEN** the messages behind it are not delivered

#### Scenario: A failed delivery is redelivered

- **WHEN** a reader negatively acknowledges a message
- **THEN** the message becomes available for delivery again

#### Scenario: Staged files outlive an unacknowledged message

- **WHEN** handling fails before acknowledgement for a message with staged temporary files
- **THEN** those files still exist for the retry

### Requirement: Published payload and delivered message are distinct types

The type accepted by `IOutbox.publish` SHALL carry payload only and SHALL be serializable. The type
returned by `IOutboxReader.poll` SHALL carry that payload together with the acknowledgement handles.
Acknowledgement handles SHALL NOT be fields of the published type, and SHALL NOT default to no-ops.
"Serializable" SHALL mean reconstructible as the types the payload declares, not merely expressible
as JSON: what a reader receives after a round trip SHALL behave identically to what was published,
as pinned by the round-trip requirement below.

#### Scenario: A published message cannot be acknowledged

- **WHEN** the published payload type is inspected
- **THEN** it exposes no acknowledgement operation and holds no callables

#### Scenario: A payload survives serialization

- **WHEN** a published payload is serialized and deserialized
- **THEN** it equals the original by value and every field holds an instance of its declared type
- **AND** the acknowledgement handles are not part of what was serialized, because they belong to the
  delivery rather than to the payload

### Requirement: A seam payload round-trips as its declared types

`InboundEnvelope` and `OutboundMessage` SHALL survive a round trip through the seam's serialization
as their declared types, not as an equal collection of JSON values. A backend SHALL reconstruct
`kind` as an `OutboundKind` member, `scope` as an `OwnerScope`, and `attachment` as an
`AttachmentRef`, and SHALL do so by constructing those types so their own validation runs on the
receiving side.

This is not a formality about tidiness. Every reader dispatches on the kind by identity
(`transport/browser/ws.py`, `transport/dialogs/transport.py`,
`transport/a2a/delivery.py`) and reads `.kind.value` for its frames and logs
(`transport/browser/ws.py`); `OutboundKind` is a `str`-mixin enum
(`transport/interfaces.py`), so a decoded plain string compares equal under `==` while `is` is
false and `.value` raises. `chat_id` and `source` are properties over the scope
(`transport/interfaces.py`), so a decoded mapping raises at the first routing
decision. The attachment is read by attribute (`transport/browser/ws.py`), and
`transport/reader_loop.py` turns a raising handler into ack-and-drop — a mapping there does not
fail the delivery, it silently swallows the photo.

#### Scenario: The message kind returns as its enum member

- **WHEN** an `OutboundMessage` published with `kind` `PHOTO` is serialized and deserialized
- **THEN** the decoded `kind` is the `OutboundKind.PHOTO` member itself, so an identity check against
  it succeeds and `.value` returns `"photo"`
- **AND** a value naming no member of `OutboundKind` fails to decode rather than arriving as a string

#### Scenario: The scope returns as an owner scope

- **WHEN** either payload type is serialized and deserialized
- **THEN** the decoded `scope` is an `OwnerScope` whose four axes equal the original's
- **AND** reading `chat_id` and `source` off the decoded payload returns the scope's `chat_id` and
  `surface_id` rather than raising

#### Scenario: An attachment reference returns as a reference

- **WHEN** an `OutboundMessage` carrying an attachment is serialized and deserialized
- **THEN** the decoded `attachment` is an `AttachmentRef` whose `attachment_id`, `filename`, `mime`
  and `size` are readable as attributes
- **AND** a payload whose attachment cannot be reconstructed fails to decode, rather than being
  delivered to a reader that will raise and have the message acknowledged and dropped

#### Scenario: The chat axis crosses the wire as a string

- **WHEN** a payload whose scope names chat `"4242"` is serialized and deserialized
- **THEN** the decoded `scope.chat_id` is the string `"4242"`
- **AND** `routed_chat_id` (`transport/interfaces.py::routed_chat_id`) remains the only *rule* by which the axis
  is converted — every site that carries the axis beside the scope calls it rather than parsing
  again — because exactly one reader needs it converted: A2A registers each pending request
  under the integer `derive_a2a_chat_id` mints for its task (`transport/a2a/executor.py::derive_a2a_chat_id`) and
  acknowledges and drops any message whose chat is not an `int`
  (`transport/a2a/delivery.py`), so an axis normalized on the wire would lose every answer
  that transport is waiting on

#### Scenario: A browser conversation survives the round trip verbatim

- **WHEN** a payload whose scope names a browser conversation with a chat id no numeric reading
  applies to is serialized and deserialized
- **THEN** the decoded `scope.chat_id` is that string, byte for byte
- **AND** nothing on the wire refuses it for its shape or folds it toward a number, because the
  browser boundary admitted it as an opaque key and two spellings there are two conversations

#### Scenario: Equality is promised and identity is not

- **WHEN** a payload is submitted or published and then polled back
- **THEN** what is returned equals what was handed over
- **AND** no caller and no conformance case SHALL require it to be the same object, because the
  in-process backend returns the object it was given and a backend spanning processes cannot

#### Scenario: A field a reader does not know does not stop delivery

- **WHEN** a payload encoded by a newer instance carries a field the decoding instance does not know
- **THEN** the unknown field is ignored and the message is delivered, because instances are upgraded
  one at a time and a strict decode would turn a rolling deploy into dropped user messages
- **AND** a known field absent from the payload takes its declared default

### Requirement: Free-form payload fields carry only JSON-safe values

`InboundEnvelope.transport`, `InboundEnvelope.attachments`, and `OutboundMessage.transport` SHALL
carry only JSON-safe values. The transport seam SHALL carry no runtime skill-repair constraint.

#### Scenario: A task crosses the transport seam

- **WHEN** an inbound message is encoded, queued, and decoded
- **THEN** its transport and attachment metadata are preserved
- **AND** no skill-repair lifecycle field is present

### Requirement: Media travels by reference

An outbound message carrying a photo or a file SHALL carry a reference resolvable through the shared
storage provider, and SHALL NOT carry the bytes inline.

#### Scenario: A photo does not inflate the queue message

- **WHEN** a photo is published
- **THEN** the queued message carries an attachment reference and its size is independent of the
  photo's size

#### Scenario: A transport resolves the reference at delivery

- **WHEN** a transport delivers a message carrying an attachment reference
- **THEN** it fetches the bytes from the storage provider at that point

### Requirement: The seam is synchronous, and a poll asks the broker for nothing beyond its work

The seam's operations — `submit`, `publish`, `poll`, `close`, `start`, `stop` — SHALL be synchronous.
`poll` SHALL take a timeout and SHALL block for at most that long, so a reader loop needs no sleep of
its own, except for the bounded round trips this requirement names below, each of which the backend
states. `close` SHALL interrupt a concurrent `poll` rather than let it run to its timeout. An
asynchronous transport SHALL bridge to its event loop on its own side.

A timeout of zero or less SHALL NOT wait for a message to arrive: `poll` SHALL return whatever is
immediately available and otherwise `None`. This is not an implementation detail of one backend. The
supervisor drains the inbox with `poll(0.0)` after its first, blocking poll, so a backend that waited
on a zero timeout would turn a drain into one message per tick — and would do it silently, because
every case that passes a real timeout still passes. A request a backend makes to settle, or to fetch
what the broker holds, is not a wait for a message, provided it is bounded
as stated below.

"Immediately available" SHALL mean what the broker already holds for that consumer at the call, not
only what a client happens to have buffered. A client that fetches in the background returns nothing
it has not fetched. A backend built on such a client SHALL make one bounded fetch on a zero timeout
and return what that fetch delivered. A consumer whose broker assigns it work through a group
holds nothing until it has joined; a zero-timeout poll that spends its bounded fetch on that join
returns `None`, and the promise applies from the first poll after the consumer holds its
assignment. The backend SHALL state the bound, and the bound SHALL be small next to any timeout a
caller passes when it means to wait: on the Kafka backend it is
`KAFKA_ZERO_TIMEOUT_FETCH_MS`, at most 100 ms. An inbox that settles the previous envelope at the top
of `poll` SHALL treat that settlement as a bounded round trip of its own, on a zero timeout as on any
other, and not as a wait for a message. A drain that skipped the settlement would settle nothing.

A backend SHALL NOT make a request only to observe an outage its client hides behind an empty fetch.
Such an outage is reported by the client's own connection logs, and a poll during it returns `None`
within its stated bounds.

`close` SHALL release what the reader holds on the broker's client before it returns, when no `poll`
is in flight on that reader. When a `poll` is in flight, a backend whose client may be used from two
threads at once MAY still release in `close` itself. A backend whose client is not thread-safe SHALL
instead mark the reader closed and wake the poller, and the polling thread SHALL release those
resources at its next slice boundary. A `poll` that begins after `close` SHALL return `None` and SHALL
NOT raise. Both paths exist for a reason. A client that is not thread-safe cannot be closed under its
poller. A transport that joins its reader thread before closing leaves no poller, so a flag nobody
reads would release nothing.

#### Scenario: Polling an empty outbox returns after the timeout

- **WHEN** `poll` is called with a timeout on an outbox with nothing to deliver
- **THEN** it returns nothing after approximately that timeout

#### Scenario: Closing releases a blocked poll

- **WHEN** `close` is called while another thread is blocked in `poll`
- **THEN** that `poll` returns without waiting out its timeout

#### Scenario: A zero timeout returns immediately

- **WHEN** `poll(0.0)` is called on an inbox or an outbox reader with nothing available
- **THEN** it returns `None` without waiting for a message, within the bounds its backend states
- **AND** when something is available it returns it, so a drain loop terminates on the first empty
  poll rather than on a timeout

#### Scenario: A zero timeout on a background-fetching client sees what the broker holds

- **WHEN** an envelope has been accepted by `submit` and `poll(0.0)` is then called on an inbox whose
  client fetches in the background and whose consumer already holds its assignment
- **THEN** that poll makes at most one fetch, bounded by the backend's stated zero-timeout bound, and
  returns the envelope
- **AND** a `poll(0.0)` with nothing to deliver and no settlement pending returns `None` within that
  bound

#### Scenario: An idle poll on a broker that is gone returns within its bounds

- **WHEN** an inbox whose client hides lost connectivity is polled with a zero timeout, repeatedly,
  while its broker does not answer
- **THEN** every poll returns `None` without raising, within the zero-timeout fetch bound plus the bound
  of a settlement still pending
- **AND** the inbox makes no request beyond its fetch and that settlement

#### Scenario: Closing a reader with no poll in flight releases it before returning

- **WHEN** `close` is called on a reader whose thread is not inside `poll`
- **THEN** what the reader holds on the broker's client is released before `close` returns
- **AND** a `poll` called afterwards returns `None` without raising

#### Scenario: A poll racing close never raises

- **WHEN** `close` runs while another thread is inside `poll`
- **THEN** that `poll` returns `None` by its next slice boundary and raises nothing
- **AND** what the reader held on the broker's client is released, by the polling thread when the
  client is not thread-safe

### Requirement: One inbound envelope type exists

`transport.interfaces.InboundEnvelope` SHALL be the only inbound envelope in the system. It SHALL
carry plain types only, together with a required minted `OwnerScope` in `scope`. The supervisor
SHALL accept it directly rather than converting from or to a second envelope type of its own.
`chat_id` and `source` SHALL be properties read from `scope` (`scope.chat_id` and
`scope.surface_id`), not second fields on the envelope.

#### Scenario: No second envelope survives

- **WHEN** the repository is searched for inbound envelope definitions
- **THEN** exactly one exists, in the seam

#### Scenario: The envelope exposes the scope's address, not its own

- **WHEN** an inbound envelope's `chat_id` and `source` are read
- **THEN** they equal the minted scope's `chat_id` and `surface_id`
- **AND** the envelope has no separate `chat_id`/`source` fields that could disagree with the scope

#### Scenario: An envelope cannot be built without a scope

- **WHEN** an `InboundEnvelope` is constructed without a `scope`
- **THEN** construction raises

### Requirement: The composition root owns transport lifecycle

The composition root SHALL start and stop every transport. Each transport keeps the lifecycle its
runtime dictates — dialogs is a self-driven poller, browser lives in the ASGI application, a2a is its
own server — so no shared lifecycle interface is imposed on them. What every transport SHALL hold is
narrower: the gateway is available before the transport begins consuming, and stopping releases the
outbox reader.

#### Scenario: A transport is started and stopped by the composition root

- **WHEN** the composition root starts and then stops a transport
- **THEN** the transport consumes the outbox only between those calls

#### Scenario: Stopping releases the reader

- **WHEN** a transport is stopped
- **THEN** its outbox reader is closed and it consumes nothing further

### Requirement: A broadcast subscription does not outlive its process

A `BROADCAST` subscription SHALL NOT outlive the process it belonged to, whether or not `close` ran.
The mode is defined as one independent subscription per instance beginning at the present moment
because an undelivered message on it belongs to a connection that died with the process; a
subscription that survives its process turns that definition into its opposite.

`close` is the orderly release and SHALL NOT be the only one. It is exactly the call a crash skips,
so an implementation SHALL release a subscription whose process did not return, without depending on
that process to release it. Two failures follow if it does not: a restarted instance is handed
messages addressed to sockets that no longer exist, and a crashed instance leaves a subscription
nothing drains, growing until retention is its only bound.

A `BROADCAST` subscription SHALL NOT degrade into competing consumers. Every instance holding the
name SHALL receive every published message, and one instance's acknowledgement SHALL NOT withhold a
message from another. A shared position hands a message to an instance holding no client for it,
which acknowledges and drops it on its own side, and the message reaches nobody.

The instance identity that separates these subscriptions, and the mechanism that detects a process
that did not return, SHALL both live in the implementation. No method SHALL be added to the seam for
a reader to announce that it is alive: the calls a live reader already makes are what the
implementation observes.

#### Scenario: A subscription whose process did not return is released

- **WHEN** an instance holding a `BROADCAST` subscription stops without calling `close`
- **THEN** the implementation releases that subscription
- **AND** messages published afterwards do not accumulate against it

#### Scenario: A restarted instance is not handed the previous process's backlog

- **WHEN** an instance holding a `BROADCAST` reader restarts and opens a reader under the same name
- **THEN** it receives only messages published after the new reader opened

#### Scenario: Broadcast does not become competing consumers

- **WHEN** two instances hold `BROADCAST` readers under one name and a message is published
- **THEN** both are delivered it
- **AND** one instance acknowledging it does not withhold it from the other

#### Scenario: The seam gains no liveness call

- **WHEN** `IOutboxReader` and `IOutbox` are inspected
- **THEN** no method exists for announcing that a reader or its instance is alive
- **AND** no transport names the instance its subscription belongs to

### Requirement: A subscription name claims one mode for every instance

The name is the consumer identity and a consumer has one mode: two modes under one name are two
subscriptions the publisher fans into, which is a duplicate delivery. The claim SHALL be visible to
every instance reading the same outbox and SHALL NOT be held per process. Opening a name in a mode
another instance already claimed SHALL be refused with an error naming the claimed mode and the
requested one, rather than silently opening a second subscription.

A per-process claim is invisible exactly when it matters. A rolling deploy runs two versions at once,
and if the new one opens a name the old one holds in the other mode, every message is delivered twice —
once to each subscription — and no instance can see the conflict.

Releasing a reader SHALL NOT release the claim: an instance that closes and reopens keeps its mode,
so a restart cannot race a peer into changing it.

How a claim is discharged follows the mode, because the claim is not a record kept beside the
subscription — it is the subscription. A `BROADCAST` claim SHALL lapse when no instance holds a
subscription under that name, since such a subscription does not outlive its process on any backend. A
`SHARED` claim SHALL lapse only where the subscription's own state does: on a backend that cannot span
processes the subscription ends with the process and the claim with it, while on a backend whose
`SHARED` progress survives a restart the retained subscription is that progress, and discarding it to
release a claim would lose the messages the mode exists to keep. On such a backend, changing a
`SHARED` name's mode SHALL therefore be an explicit operation that deletes the retained subscription,
and the refusal SHALL keep naming both modes until it is performed.

A backend that can open only one mode meets this requirement structurally. No instance can open a
second mode under any name, so two subscriptions under one name cannot exist and there is no claim
to record, read or lose. Such a backend SHALL NOT keep a claim record. Its refusal names the
unsupported mode and the backend rather than a claimed mode (see *A reader declares a name and a
subscription mode*). The rules above for recording and discharging a claim bind every backend that
can open both modes.

#### Scenario: A second instance cannot open one name in the other mode

- **WHEN** one instance holds a name as `BROADCAST` and another instance opens the same name as
  `SHARED`
- **THEN** the second open fails with an error naming both modes, or, on a backend that cannot open
  `SHARED` at all, naming `SHARED` and the backend
- **AND** no second subscription exists under that name

#### Scenario: A rolling deploy cannot double-deliver

- **WHEN** two versions of a transport run at once and the newer one declares a different mode for
  the name the older one holds
- **THEN** the newer instance fails to open its reader
- **AND** no published message is delivered to two subscriptions under that name

#### Scenario: Closing a reader keeps the mode claimed

- **WHEN** an instance on a backend that can open both modes closes its only reader for a name and
  immediately opens another
- **THEN** the name still holds the mode it was first opened with
- **AND** opening it in the other mode is still refused

#### Scenario: A broadcast claim does not outlive every instance that held it

- **WHEN** no instance holds a `BROADCAST` subscription under a name any more
- **THEN** a later instance may open that name in either mode the backend supports

#### Scenario: A shared claim persists with the progress it names

- **WHEN** every instance holding a `SHARED` name is stopped on a backend that opens `SHARED` and
  whose subscriptions span processes, and a later instance opens that name in the other mode
- **THEN** the open is refused naming both modes, because the retained subscription still holds the
  unacknowledged progress the mode guarantees
- **AND** the retained subscription is not discarded in order to release the claim

#### Scenario: A single-mode backend keeps no claim record

- **WHEN** readers under one name are opened and closed on a backend that supports only `BROADCAST`,
  including by two versions of a transport during a rolling deploy
- **THEN** each open either succeeds as `BROADCAST` or is refused as the unsupported mode
- **AND** nothing on the broker or beside it records which mode the name holds

### Requirement: Publishing is all-or-nothing and reports failure by raising

`IOutbox.publish` SHALL make the message available to every subscription open at the time of the
call, or to none of them. It SHALL return `None` on success and SHALL raise when the message was not
taken. It SHALL NOT report partial success, and an implementation SHALL NOT be structured so that a
publish is several independent writes whose failure can leave some subscriptions holding the message
and others not.

The reason is that a partial fan-out has no repair. The seam identifies a delivery, not a message —
a receipt names one attempt (`transport/interfaces.py`) — so nothing can recognise a
republished message as one the surviving subscriptions already hold; republishing duplicates it and
doing nothing leaves an answer that one instance's clients see and another's never will, with no
component whose job is to notice. Making the state unreachable is cheaper than reconciling it, and
it constrains an implementation to a single append with fan-out by consumer position.

Atomicity is relative to the set of subscriptions open when `publish` is called, not to a moment in
time: a subscription opened during or after the call SHALL NOT be considered to have missed it, since
`BROADCAST` begins at the present in any case.

Failure by exception is the same convention `IInbox.submit` already uses, so a caller retries from
its own record on either half of the seam and a fake that forgets the rule fails loudly rather than
silently dropping messages.

A publish that raised SHALL have reached no subscription, with one declared deviation. A backend that
takes it SHALL declare it, and what it costs a caller that retries, in the architecture document's
description of that backend. On a broker whose append cannot be cancelled once
it is handed to the client, a publish that raised because a bounded wait for the broker's answer
expired, whether the backend's own wait or the client's delivery timeout, MAY still land afterwards
and be delivered. The deviation covers every publish, with or
without a `before_publish` budget, because in both cases the wait that expires is the backend's own.
It costs a duplicate wherever the caller retries from its own record: a terminal answer published
again, an echo sent again, a skill's message sent twice. The deviation does not cover two cases:

- A publish the broker refused, such as an authorization failure or a message over the size
  ceiling, reached nobody on every backend.
- A `before_publish` that raises, or reports no remaining budget, before the message is handed to
  the client SHALL end the publish with nothing handed over.

A backend that takes the deviation SHALL NOT close the gap with a mechanism whose wait it cannot
bound by the caller's budget, such as a broker transaction whose commit or abort waits without a
timeout.

#### Scenario: A publish that raises reached nobody

- **WHEN** a publish fails and raises because the append was refused, or was never handed to the
  broker
- **THEN** no open subscription delivers that message to any reader, then or later

#### Scenario: An expired authority hands nothing to the broker

- **WHEN** `before_publish` raises, or reports no remaining budget, before the message is handed to
  the broker's client
- **THEN** `publish` raises
- **AND** no open subscription delivers that message to any reader, then or later

#### Scenario: A publish whose wait expired may land on a broker that cannot cancel an append

- **WHEN** a backend that declares an uncancellable append raises from `publish` because its bounded
  wait expired, and the broker then accepts the append it was already handed
- **THEN** the subscriptions open at the call MAY deliver the message
- **AND** the architecture document's description of that backend names this deviation and what it
  costs a caller that retries

#### Scenario: A publish that returns reached every open subscription

- **WHEN** `publish` returns without raising while several subscriptions are open
- **THEN** each of them delivers the message to a reader

#### Scenario: Partial success is not a result the interface can express

- **WHEN** `IOutbox.publish` is inspected
- **THEN** it returns `None` and its only failure signal is a raised exception
- **AND** no return value, callback, or out-parameter reports which subscriptions took the message

### Requirement: Retention is bounded, and a publisher that can read the consumers' positions names the one that fell behind

The outbox SHALL bound what it retains for unread messages rather than growing until the process
dies. When the bound is reached it SHALL drop the oldest retained message and SHALL NOT refuse the
publish, because a publish that fails on another consumer's slowness would make an unrelated
producer's turn fail. Where the publisher can read each subscription's position, a drop SHALL be reported at error level,
rate-limited rather than once per message, and the report SHALL name the consumer whose position is
furthest behind — the consumer that has stopped consuming is the actionable fact, and a bound stated
over one shared sequence can no longer name a per-subscription backlog.

A `BROADCAST` consumer SHALL treat the drop as correct rather than degraded: an undelivered message
belongs to a connection that died with the process. A `SHARED` consumer's drop is a lost message, and
the diagnostic is what makes it visible.

An implementation MAY meet this bound per subscription, provided its diagnostic still names the
consumer.

The bound MAY be the broker's own time or size retention rather than an exact count inside the
append. A backend that uses it SHALL state its granularity and timing: a log that deletes whole
segments asynchronously keeps more than the bound for a while, then drops the oldest segment at once.
It SHALL still drop the oldest, and SHALL still never refuse the publish.

A subscription whose position only its reader holds, such as a `BROADCAST` reader that keeps its
position in its own process, SHALL resume at the oldest retained message without raising out of `poll`
when the position it asks for is below the oldest retained message, or past the newest because the log
was recreated or truncated. Such a backend reports neither case, and SHALL state so in the architecture
document's description of that backend: a reader of a live transport keeps up with the retention, and
one whose transport stopped consuming for longer has already lost the connections it served.

#### Scenario: An overflowing outbox drops the oldest, not the newest

- **WHEN** more messages are retained than the bound allows
- **THEN** the oldest retained message is dropped and the newest is kept

#### Scenario: The drop names the consumer that stopped consuming

- **WHEN** a drop occurs because one consumer has stopped reading, on a backend whose publisher can read
  that consumer's position
- **THEN** an error-level report identifies that consumer at the drop
- **AND** repeated drops do not produce one report per dropped message

#### Scenario: A stalled consumer does not fail a publish

- **WHEN** a consumer is far enough behind that publishing drops an older message
- **THEN** `publish` still returns without raising

#### Scenario: A reader whose position left the log resumes without raising

- **WHEN** a reader whose position only it holds asks for a message the broker no longer retains, or for
  a position past the newest message because the topic was recreated
- **THEN** its `poll` resumes at the oldest retained message without raising

### Requirement: A backend over partitioned topics keeps each direction on one partition

A backend whose inbound or outbound topic may be divided into several partitions, by an infrastructure the
deployment does not control, SHALL append every inbound envelope to one fixed partition of its inbound
topic and every outbound message to one fixed partition of its outbound topic, and its inbox and outbox
readers SHALL take their messages from that partition. A client left to place a record that has no key
picks a partition itself: a reader of one partition then never sees what landed on another, and an inbox
group hands envelopes out of submission order and to more than one instance at once. The Kafka backend
uses partition 0, which every topic has, and places no requirement on the partition count.

#### Scenario: An outbound topic with several partitions delivers every message in order

- **WHEN** the outbound topic has several partitions, a `BROADCAST` reader is open, and several messages
  are published
- **THEN** the reader receives every message, in publication order

#### Scenario: An inbound topic with several partitions is polled in submission order

- **WHEN** the inbound topic has several partitions and several envelopes are submitted
- **THEN** the inbox returns every envelope, in submission order

### Requirement: A poll settles the envelope the previous poll returned

`IInbox.poll` SHALL settle the envelope it returned from the same inbox on its previous call, and
SHALL then return the next envelope or `None`. At most one envelope per inbox SHALL be unsettled at
any moment, and an inbox SHALL NOT return a second copy of the envelope it currently holds unsettled.
Because in flight is capped at one, an implementation SHALL need no receipt, no handle, and no
in-flight map to know what a poll settles.

An envelope that was returned and never settled SHALL be delivered again to a consumer of that inbox.
Inbound is therefore at-least-once, bounded to one message: a consumer that stops — crash, restart,
deployment roll — before its next poll leaves exactly that envelope for its replacement.

A consumer SHALL finish with the envelope it holds before polling again, because the next poll is the
settlement. A consumer that polls ahead of its own handling settles messages it has not handled, and
loses them exactly when it stops.

Settlement SHALL NOT depend on how handling ended. An envelope whose handling raised is settled by
the next poll like any other: the queue is not a retry engine for application errors, a message that
fails deterministically would otherwise be redelivered forever, and the consumer already answers the
user itself. Redelivery covers a consumer that vanished, not one that failed.

#### Scenario: The next poll settles the previous envelope

- **WHEN** a consumer polls an envelope and then polls again
- **THEN** the second call returns the next submitted envelope, or `None` when none was submitted
- **AND** the first envelope is settled and is never returned again to that inbox

#### Scenario: A consumer that stops before its next poll sees the envelope again

- **WHEN** a consumer polls an envelope and is replaced without ever polling again
- **THEN** the replacement's first poll returns that same envelope

#### Scenario: Only one envelope is unsettled at a time

- **WHEN** the inbound half of the seam is inspected for in-flight bookkeeping
- **THEN** an inbox holds at most one unsettled envelope
- **AND** no receipt, delivery handle, or in-flight map exists on the interface or in the backend to
  identify which envelope a settlement refers to

#### Scenario: Handling that raised is settled anyway

- **WHEN** a consumer's handling of a polled envelope raises and the consumer polls again
- **THEN** that envelope is settled and is not delivered again
- **AND** nothing in the queue retries it

### Requirement: Redelivery binds a backend that can outlive its consumer

The redelivery obligation SHALL be the contract for any backend whose queue can span processes, which
is where the guarantee is needed: a transport and a supervisor deployed separately lose messages in
exactly this gap. A backend that cannot outlive its consumer SHALL still settle on the next poll, so
that no envelope is returned twice, and SHALL document at the backend itself which half of the
contract it does not hold.

The conformance case for redelivery SHALL be gated on a capability the backend declares rather than
removed, so it is skipped with a stated reason on an in-memory backend and runs unchanged against the
first backend that claims to span processes. A deleted case would leave that backend's first
implementation untested by construction.

On a broker that manages group membership, the unsettled envelope SHALL reach the replacement once
the broker observes that its holder departed, and not before:

- at once, when the holder leaves in order;
- after the session timeout, when the holder crashed;
- after the session timeout, when a live holder's heartbeats stopped reaching a broker that stayed up,
  as in a network partition;
- after the poll interval, when a live holder stopped polling.

The last two cases are how a live consumer loses an envelope it still holds: a redelivery of the
same class, collapsed on the task id. The broker has already removed that consumer from the group,
so its next settlement SHALL be refused as stale rather than retried. The envelope then goes to
whichever member holds the partition after the rejoin, which may be the same inbox. This is the one
case in which *A poll settles the envelope the previous poll returned* returns a polled envelope to
the inbox that polled it, and it happens only on a backend that manages group membership.

The conformance hook that builds a replacement consumer SHALL model a holder that departed, by
closing or crashing the original. It SHALL NOT add a second live member beside the original, because
a live member keeps its partition, and nothing is redelivered while it does.

#### Scenario: The in-process backend states what it cannot do

- **WHEN** the in-process inbox is inspected
- **THEN** it settles the previously returned envelope on the next poll and never returns one twice
- **AND** it documents that an unsettled envelope is lost with the process, because its queue does
  not outlive the consumer that would be redelivered to

#### Scenario: A backend that cannot span processes skips the case with a reason

- **WHEN** the inbox conformance suite runs against a backend that declares it cannot outlive its
  consumer
- **THEN** the redelivery case is skipped on that declared capability, naming the backend as the
  reason, and every other case runs

#### Scenario: A backend that can span processes runs the case

- **WHEN** the inbox conformance suite runs against a backend that declares it can outlive its
  consumer
- **THEN** the redelivery case executes and requires the unsettled envelope to reach a fresh consumer
  over the same backend

#### Scenario: A group-managed backend redelivers once the holder has departed

- **WHEN** an inbox consumer on a broker that manages group membership takes an envelope and departs
  without settling it, by an orderly leave or by a crash
- **THEN** a replacement consumer in the same group receives that envelope once the broker observes
  the departure
- **AND** the conformance hook that builds that replacement closes or crashes the original consumer
  first

#### Scenario: A holder that stops polling past the poll interval loses its envelope

- **WHEN** a live consumer on such a broker keeps an envelope unsettled for longer than its poll
  interval
- **THEN** the broker hands the envelope to a peer, which receives it again
- **AND** the settlement the original consumer attempts afterwards is refused as stale rather than
  retried, and its next poll rejoins the group

#### Scenario: A holder whose session expired gets its envelope back after the rejoin

- **WHEN** a live consumer on such a broker holds an envelope, the broker stays up but stops receiving
  that consumer's heartbeats for longer than the session timeout, and the consumer is the only member
  of its group when they resume
- **THEN** its next settlement is refused as stale and is not recorded as an outage
- **AND** a later poll of the same inbox returns that envelope again, and the task id collapses the
  duplicate

The transport conformance suite (`tests/helpers/transport_conformance.py`) verifies every backend against the seam contract. Add new backends by implementing the conformance base class, not by copying tests.
