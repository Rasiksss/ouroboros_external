# operation-tracing Specification

## Purpose

One identifier per operation, carried end to end. It is accepted at every ingress whose protocol
delivers one and originated where none does, normalized to a single spelling, crossed as data at
every boundary that does not inherit a context, stamped on every outbound call whose transport can
carry a header or per-call metadata, and written to the log record. What it answers is *which
operation*; the agent identifier alongside it answers *which agent*.

## Requirements

### Requirement: One operation identifier, in one normalized form

The system SHALL identify every operation with a single identifier that is unchanged for the whole
chain of calls belonging to one parent task, and SHALL carry it in the `X-Trace-Id` header.

An identifier the system generates SHALL be a version 4 UUID. An identifier the system accepts SHALL be
either the canonical dashed `8-4-4-4-12` form or the 32-character unseparated hexadecimal form, and
SHALL be normalized to the lowercase canonical dashed form before it is stored, emitted, mirrored into
the framework's own context, or written to a log record. Exactly one spelling of an identifier SHALL
reach any consumer, and it SHALL be the dashed one.

Normalization SHALL NOT remove the separators from an identifier that carries them. A caller that sends
the conventional spelling of a UUID SHALL find that same value echoed back; a caller that sends the
unseparated spelling SHALL have the separators restored rather than the identifier passed through.

The system SHALL NOT reject an accepted identifier on account of its UUID version. Generation is fixed
at version 4; acceptance is strict about shape and permissive about version, because a caller's
identifier is valid for the caller's own purposes and rejecting it defeats the correlation the header
exists for.

#### Scenario: The canonical dashed form is accepted unchanged

- **WHEN** a caller supplies `f47ac10b-58cc-4372-a567-0e02b2c3d479`
- **THEN** the operation runs under `f47ac10b-58cc-4372-a567-0e02b2c3d479`
- **AND** no replacement identifier is generated

#### Scenario: The unseparated form has its separators restored

- **WHEN** a caller supplies `f47ac10b58cc4372a5670e02b2c3d479`
- **THEN** the operation runs under `f47ac10b-58cc-4372-a567-0e02b2c3d479`
- **AND** no replacement identifier is generated

#### Scenario: One spelling reaches every consumer

- **WHEN** an operation runs under an identifier supplied in either spelling
- **THEN** the outbound header, the log record, and the framework context all carry the dashed form

#### Scenario: A generated identifier is a version 4 UUID

- **WHEN** the system generates an identifier
- **THEN** it is a version 4 UUID in the canonical dashed form

#### Scenario: A version other than 4 is accepted from a caller

- **WHEN** a caller supplies a well-formed UUID that is not version 4
- **THEN** the operation runs under that identifier

### Requirement: Rejecting a supplied identifier is logged; reading one back is not

Parsing an identifier SHALL be separated from accepting one at a boundary.

The parsing operation SHALL be total and silent: it SHALL return the normalized form of a well-formed
value and an empty result for anything else, and SHALL be what the system uses to read back an
identifier it previously wrote — a stored task record, a queued event.

The accepting operation SHALL be what an ingress uses. Given a value that is present and cannot be
normalized, it SHALL log a warning naming the boundary that rejected it, and SHALL then generate a new
identifier. Given an absent value it SHALL generate a new identifier without a warning, because no
caller spoke.

The warning SHALL NOT include the rejected value. It arrives as unvalidated input of unbounded length on
paths that are not authenticated, and the operator's actionable fact is which boundary is receiving
identifiers the system will not accept.

A rejection SHALL be logged once per value a caller supplied, not once per use of it. Where a carrier is
fixed for the life of a connection and read by many messages, it SHALL be accepted once when the
connection is established, because re-reading it would restate one caller's single mistake for every
frame that follows — unbounded volume from an ingress that is not authenticated.

#### Scenario: A malformed supplied identifier is substituted visibly

- **WHEN** a caller supplies an identifier that cannot be normalized
- **THEN** a warning is logged naming the boundary
- **AND** the operation runs under a newly generated identifier

#### Scenario: An absent identifier is not a warning

- **WHEN** a request arrives carrying no identifier
- **THEN** an identifier is generated
- **AND** no warning is logged

#### Scenario: Reading back a stored identifier is silent

- **WHEN** a task record or a queued event is read back and its identifier is absent or unusable
- **THEN** an identifier is established without a warning

#### Scenario: A rejection on a connection-fixed carrier is logged once

- **WHEN** a connection is established carrying a malformed identifier in its handshake, and many
  messages then arrive on it
- **THEN** exactly one rejection is logged, when the connection is established

#### Scenario: The rejected value is not written to the log

- **WHEN** a rejection is logged
- **THEN** the record names the boundary and states that a value was rejected
- **AND** the value itself does not appear in the record

### Requirement: An ingress accepts an identifier when its protocol carries one, and originates when it does not

Every transport ingress SHALL establish the operation's identifier explicitly. It SHALL NOT open a scope
whose identifier is decided by a default argument.

A transport with more than one carrier SHALL cover every one of them. A transport whose HTTP surface
accepts requests alongside another protocol SHALL accept the identifier on that surface too: the header
arrives whether or not anything reads it, and an unread one is the failure that leaves no evidence —
no rejection warning, no field on the log record, and no header on the storage and service calls the
handler goes on to make.

A transport whose protocol carries an inbound identifier SHALL read it from that carrier and accept it.
A transport whose protocol has no such carrier SHALL originate the chain: it SHALL generate an
identifier through the same accepting operation, so that origination is a named act rather than the
absence of one. A transport SHALL NOT be given a carrier field for an identifier its protocol cannot
deliver.

#### Scenario: A browser client's identifier is accepted

- **WHEN** a browser message carries an identifier in its body, or the connection's handshake carries one
  in its headers
- **THEN** the operation runs under it

#### Scenario: A browser client's identifier is accepted on the HTTP surface as well

- **WHEN** a request carrying the header reaches one of the transport's HTTP routes
- **THEN** the handler runs under it, and the storage calls it makes carry it onward
- **AND** a request carrying no header runs under a newly originated identifier rather than under none

#### Scenario: An HTTP scope does not outlive its request

- **WHEN** a request handled under an accepted identifier completes
- **THEN** no identifier remains established, so later unrelated work is not attributed to that caller

#### Scenario: An A2A caller's identifier is accepted

- **WHEN** an A2A request arrives carrying the header
- **THEN** the operation runs under the identifier it carries
- **AND** no new identifier is generated

#### Scenario: A transport with no carrier originates

- **WHEN** a message arrives on a transport whose protocol carries no identifier
- **THEN** the ingress generates one and the chain begins there
- **AND** the generated identifier reaches the supervisor on the inbound envelope

#### Scenario: No ingress leaves the identifier to a default

- **WHEN** every scope opened under the transport package is inspected
- **THEN** each is given an identifier explicitly
- **AND** none relies on the argument's default value

### Requirement: An identifier crosses a boundary as data

An identifier SHALL cross as data — a field, a state entry, or an argument — and SHALL be
re-established on the far side, wherever control passes to a context that does not inherit the caller's
context variables. The context variable SHALL be treated as a convenience within one context and never
as a carrier between two.

This SHALL hold at four boundaries: the queue between a transport and the supervisor, which carries the
identifier as a field on the inbound envelope; the outbox queue between the supervisor and the transport
that delivers the answer, which carries it as a field on the outbound message and re-establishes it
around the delivery; the HTTP request handler that invokes an A2A executor, which carries the request's
headers on the call context it builds per request; and the bridge from a worker thread or a coroutine to
another event loop, which is specified in `tool-runtimes`.

A delivery SHALL NOT originate an identifier for a message that names none. A transport's delivery
thread is not an ingress, and an identifier minted there would name an operation no other record
mentions.

An ingress SHALL NOT depend on a third-party default to populate the carrier it reads. Where a library
offers an extension point for building that carrier, the system SHALL supply its own implementation, so
that the ingress does not silently stop working when the library's default changes.

#### Scenario: The identifier survives the queue

- **WHEN** a transport submits an inbound message under an active identifier
- **THEN** the envelope carries that identifier
- **AND** the task the supervisor creates runs under it

#### Scenario: The identifier survives the outbox

- **WHEN** a message published under an active identifier is delivered by a transport
- **THEN** the outbound calls the delivery makes carry that identifier
- **AND** a message published under no identifier is delivered under none rather than a generated one

#### Scenario: The identifier survives the request handler

- **WHEN** an A2A request is dispatched to the executor
- **THEN** the request's headers are available on the call context the executor holds

#### Scenario: The carrier is populated by the system, not by a default

- **WHEN** the A2A ingress is constructed
- **THEN** it supplies its own builder for the per-request call context

### Requirement: Every outbound call whose transport can carry the identifier carries it

The system SHALL add the `X-Trace-Id` header to every outbound request made while an identifier is
active, across every integration, whether or not the integration is reached through the framework SDK.
This SHALL cover model calls, MCP servers reached over HTTP, outbound agent-to-agent calls, search
services, content-inspection services, and object storage.

The mechanism SHALL follow the client library rather than being restated per integration: whatever
per-request extension point the library offers is where the header is added. Adding it SHALL set that
one header and SHALL leave the rest of the request untouched: a request may legitimately repeat a
header name, and a mechanism that rewrites the header set collapses those repetitions. Where adding the header
before a request is signed would let an intermediary that rewrites or drops it invalidate the
signature, the header SHALL be added after signing, so that carrying the identifier cannot fail the
operation it describes.

Where a client outlives one operation, the header SHALL be computed for each request rather than fixed
when the client is constructed. A client built once per connection SHALL NOT carry an identifier chosen
at construction, because it would attribute every later operation on that connection to the first one.

This SHALL hold however the request reaches the wire. Where an integration sends from a worker owned by
the connection rather than from the caller, the integration SHALL establish an operation for the
connection itself and SHALL attribute to it every request **the connection originates** — opening the
session, listing what the remote offers, and each keepalive. Those requests have no caller; work with
no caller belongs to the connection.

Where such a worker can nevertheless observe the context of the task that queued a request — because
the hand-off to it carries that context rather than discarding it — a request issued **by a caller**
SHALL carry the caller's identifier, per the general rule above. The caller being invisible at the
point the request is built is a property of the transport, not a property of connection-scoped
integrations, and SHALL NOT be assumed: an integration whose transport starts preserving that context
SHALL start attributing calls to their callers rather than continuing to attribute them to the
connection.

Such a connection-scoped identifier SHALL be generated when the connection is established and SHALL
NOT be read from whatever operation is ambient at that moment. An inherited identifier would attribute
every request the connection originates to whichever operation happened to be running when it was
opened or re-opened, which for a connection that outlives the operation is indistinguishable from
fixing the header when the client is constructed — the defect this requirement exists to prevent. It
SHALL still be read per request rather than merged into the client, because a connection is
re-established without the client being rebuilt.

Where a call *is* attributed to its connection rather than to its caller, that is a loss of
granularity and SHALL be recorded as one: such a call is correlated with the connection's other traffic
and not with the operation that issued it. It SHALL NOT be extended to an integration whose caller is
visible where the request is built; those carry the caller's identifier.

An integration whose transport is not HTTP but carries per-call metadata SHALL carry the identifier in
that channel, under the same header name spelled as the protocol requires. The chat transport's gRPC
calls SHALL carry it as request metadata and SHALL read it per call, because one client serves every
operation.

This SHALL cover the calls that read as well as the calls that write. A request the system makes to
fetch history, to resolve a participant's name or a peer, or to ask who sent a message is a request it
makes, and it fails in ways that are reconstructed after the fact rather than reported at the time.

Where such an integration reaches the same remote over more than one protocol, each call SHALL use the
channel its own protocol provides rather than being omitted for not fitting the first one: a file
transfer carried over HTTP alongside a gRPC control plane SHALL carry the identifier as an HTTP header.

A long-lived subscription the system opens to receive messages SHALL identify itself when it is opened,
under an identifier generated for that connection and never inherited from whatever operation opened it.
Its metadata is fixed for the life of the stream and cannot be re-read per message, which is exactly why
it SHALL name the connection and nothing else. A message arriving on such a subscription is data rather
than a request the system made, and SHALL be given its own identifier where it arrives; the connection's
SHALL NOT become it.

Where losing the identifier on a call would cost the system the call itself, carrying it SHALL degrade
rather than fail: an integration that will not accept the identifier SHALL be used without it, once and
on the record, rather than retried until the path it serves stops working.

Where a client library makes requests of its own — establishing a session, authenticating, registering —
the system SHALL give that client a default set of identifiers to send on any request it does not stamp
per call, so that the traffic which exists before any operation does still names the agent and the
connection. That default SHALL carry an identifier minted for the connection rather than the operation
that happened to build the client, and SHALL be overridden per call wherever the system has an operation
of its own to name.

An integration whose transport has no header frame and no per-call metadata channel SHALL be excluded
explicitly rather than silently, and the exclusion SHALL name what is excluded and why. **No integration
in this system currently meets that description.** The one that did — MCP servers reached over `stdio`,
child processes speaking JSON-RPC over pipes whose only configuration channel is fixed at spawn — was
retired with the transport itself.

The rule is kept rather than deleted alongside its instance. A transport with no per-call channel added
later must be excluded on the record, and a rule that lives only as long as it has an example would not
be there to require it.

#### Scenario: A long-lived client sends a different identifier per operation

- **WHEN** two operations with different identifiers use the same outbound connection whose requests
  are built in the caller's own context
- **THEN** each request carries its own operation's identifier

#### Scenario: A connection attributes the requests it originates to itself

- **WHEN** a connection whose requests are built by a worker it owns is established
- **THEN** the requests that established it and the requests it makes on its own schedule carry the
  connection's own identifier
- **AND** they carry it whether or not any operation was active when the connection was opened

#### Scenario: A call over such a connection carries its caller's identifier

- **WHEN** a call is made under some operation over a connection whose worker can observe the context
  of the task that queued the request
- **THEN** the request carries that operation's identifier
- **AND** it does not carry the connection's own identifier

#### Scenario: A keepalive belongs to the connection, not to the last caller

- **WHEN** a connection sends a keepalive after having served a call under some operation
- **THEN** the keepalive carries the connection's own identifier
- **AND** it does not carry the identifier of the call it followed

#### Scenario: A connection re-opened during an operation does not adopt it

- **WHEN** a connection is re-established while an unrelated operation is active
- **THEN** the identifier it establishes for itself is a newly generated one
- **AND** no request that connection originates carries the unrelated operation's identifier

#### Scenario: Every HTTP integration carries the header

- **WHEN** a request is made under an active identifier to a model, an MCP server over HTTP, a remote
  agent, a search service, a content-inspection service, or object storage
- **THEN** the request carries `X-Trace-Id` with the active identifier

#### Scenario: A streamed upload carries the identifier

- **WHEN** an object is uploaded through the storage library's streaming path, which by default
  transfers on workers of its own
- **THEN** the request carries the uploading operation's identifier, not an absent one

#### Scenario: Stamping the header leaves a repeated header intact

- **WHEN** an outbound request already carries the same header name more than once
- **THEN** the request reaches the wire with those values still separate

#### Scenario: Carrying the identifier cannot fail the request

- **WHEN** an outbound request is signed and the identifier is not part of what was signed
- **THEN** an intermediary that drops or rewrites the header leaves the request valid

#### Scenario: A non-HTTP outbound call carries the identifier in its own metadata channel

- **WHEN** the chat transport sends a message, replies, sends media, edits a progress message,
  updates presence, or asks the server for history, for a participant's name, or for the author of a
  message, while an identifier is active
- **THEN** the call carries the identifier as request metadata under the header's name
- **AND** a file transferred over the same integration's HTTP channel carries it as an HTTP header

#### Scenario: The client library's own calls are identified

- **WHEN** the library authenticates or registers while opening its connection, without the system
  stamping those requests itself
- **THEN** they carry the connection's identifier and the agent's, from the defaults the client was given
- **AND** a request the system does stamp carries its own operation's identifier instead

### Requirement: Every outbound call names the agent that made it

The system SHALL add an `X-Agent-Id` header, carrying its own configured identifier, to every outbound
request that carries `X-Trace-Id`, through the same per-request mechanism and never a value fixed when
the client is built. The two answer different questions — *which operation* and *which agent* — and the
environment's own trace has a field for each, so a request that carries one and not the other is half of
a correlation.

The identifier SHALL be read from the system's configured agent identifier, and SHALL accept the
deployment platform's own spelling of that variable as a fallback, because the contract the platform
publishes names that variable rather than the system's.

Where no identifier is configured, the header SHALL be absent rather than present and empty. An absent
header and an empty one are not the same fact: the first records that nothing was said, the second is a
value that passes validation and identifies nobody.

This SHALL NOT be confused with the identifier the surrounding infrastructure reports from its own
environment. A trace collected outside the process may carry both an environment-sourced agent
identifier and a header-sourced one; populating the first says nothing about the second, and only the
second is this system's to send.

#### Scenario: An outbound request carries both identities

- **WHEN** a request is made under an active operation by a system with a configured agent identifier
- **THEN** it carries `X-Trace-Id` with the operation and `X-Agent-Id` with the agent

#### Scenario: The platform's own variable is accepted

- **WHEN** only the deployment platform's spelling of the agent identifier is set
- **THEN** outbound requests carry that value

#### Scenario: An unconfigured agent sends no header

- **WHEN** no agent identifier is configured
- **THEN** outbound requests carry no `X-Agent-Id` at all

#### Scenario: A non-HTTP outbound call carries the agent identifier in its own channel

- **WHEN** the chat transport makes a gRPC call under an active operation
- **THEN** the request metadata carries both identifiers, spelled as that protocol requires

### Requirement: The identifier appears in the log record

A log record emitted while an identifier is active SHALL carry it as `trace_id`, alongside the task and
correlation identifiers already present. It SHALL be omitted when no identifier is active. It SHALL NOT
replace either existing identifier: the three answer different questions and are read together when an
incident is reconstructed.

#### Scenario: A log line written during an operation carries its identifier

- **WHEN** a record is emitted while an identifier is active
- **THEN** the formatted record carries `trace_id`
- **AND** it also carries the task and correlation identifiers where those are set

#### Scenario: A log line written outside an operation omits it

- **WHEN** a record is emitted with no identifier active
- **THEN** the formatted record has no `trace_id` field

### Requirement: Work that leaves its thread carries the identifier as data

Every hand-off that leaves the originating thread SHALL carry the operation identifier on the item
handed over, and the receiving side SHALL establish it around the work before any log record is
written or any outbound request is built. This covers a queue consumed by a worker, a runtime loop in
a thread of its own, and a thread pool: the identifier lives in a context variable, which is copied
when a task is created and is not shared across threads.

A carrier SHALL declare a field for the identifier. Re-deriving it on the far side, looking it up from
ambient context, or reconstructing it from another identifier SHALL NOT be used in its place.

Where the receiving side would otherwise run with no identifier, it SHALL generate one rather than
run without. An outbound client omits the header when no identifier is active rather than failing, so
work that runs unidentified loses correlation with no error, no failing test, and no evidence in the
log that anything was lost — the failure this requirement exists to prevent.

This requirement is about the boundary, not about any one crossing of it: a hand-off added later
without a carrier field is a silent correlation hole by the same mechanism, whatever it hands off.

A hand-off to a worker the *connection* owns rather than the operation is the one exception, and it
SHALL be excluded explicitly rather than by omission: such a connection establishes an operation of
its own and attributes its requests to that, per the connection-scoped rule above. The distinction is
whose lifetime the worker follows — a worker created per unit of work carries that work's identifier;
one created with a connection that outlives every operation cannot, and says so.

#### Scenario: A worker thread runs under the caller's operation

- **WHEN** work is handed from a thread running under an operation to a worker thread
- **THEN** the item handed over carries the operation identifier
- **AND** the worker performs the work under that identifier

#### Scenario: An outbound call from off-thread work is correlated

- **WHEN** work running on a thread other than the one that originated the operation makes an
  outbound HTTP request
- **THEN** the request carries the originating operation's identifier in the `X-Trace-Id` header

#### Scenario: Off-thread work with no supplied identifier runs under a generated one

- **WHEN** a hand-off arrives carrying no operation identifier
- **THEN** the receiving side generates one and runs the work under it
- **AND** no outbound request from that work is sent without the header

#### Scenario: The carrier declares the field

- **WHEN** a carrier that crosses a thread boundary is inspected
- **THEN** it declares a field for the operation identifier

### Requirement: A log record is legible independently of what decodes it

A serialized log record SHALL be legible without agreement on an encoding: non-ASCII text SHALL be
escaped so the record is pure ASCII on the wire, and no character in a message SHALL depend on the
consumer decoding the stream as UTF-8 to read correctly. A log record is read where incidents are
investigated, which is not the process that wrote it.

This applies to the whole record, message and context fields alike. A record whose text is mangled by
the consumer is a record that cannot be searched for, which costs most in exactly the incident it was
written for.

#### Scenario: Non-ASCII text survives the round trip

- **WHEN** a log record whose message contains non-ASCII text is emitted in the structured format
- **THEN** the serialized line contains no byte above ASCII
- **AND** parsing the line yields the original text unchanged

#### Scenario: An operator's search finds the record

- **WHEN** a record containing non-ASCII text is parsed by a consumer that does not decode as UTF-8
- **THEN** the message reads the same as it did at the point it was written
