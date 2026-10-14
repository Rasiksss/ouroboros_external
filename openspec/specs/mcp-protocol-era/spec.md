# mcp-protocol-era Specification

## Purpose

Which MCP protocol revision a connection speaks, how that is decided, and what may and may not
depend on the answer. The era is negotiated per connection from what the server answers on the
wire — never configured per server — and re-decided on every reconnect, so one process can hold
sessions on either revision at the same time. What the era gates is narrow and named here: the
idle keepalive, which the newer revision removed. What it must never gate is the machinery that
keeps a session alive — supervision, the reconnect ladder, the dead-session verdict, and the
detector that notices a session which failed without raising. Because that detector replaces a
keepalive the newer revision no longer has, its coverage is specified rather than left to the
client library's shape. Connection ownership, transport selection and tool dispatch belong to
`tool-execution` and are preserved by this capability, not redefined here.

## Requirements

### Requirement: The protocol revision is negotiated per connection, from what the server answers

An MCP connection SHALL decide which protocol revision it speaks by asking the server, at the moment
the connection is opened, rather than by reading configuration. The decision SHALL be made again on
every reconnect, so a server redeployed with different support changes the era of its next session
and of nothing else.

The client SHALL NOT hold a configured protocol version per server. The server states what it speaks
on the wire; a second statement of the same fact in the server list would be a second source of
truth about it, and the two would disagree the day the server is upgraded.

A connection SHALL adopt the newer, stateless revision only on **positive evidence** that the peer
speaks it — an answer to the discovery probe naming a revision this client knows. Everything else —
an unknown method, a transport-level rejection, a malformed answer, a timeout — SHALL fall back to
the handshake, so that an unreachable or older server reaches the client's existing behaviour rather
than a failure.

A transport error SHALL propagate as itself rather than being read as an era verdict: an outage is
not evidence about a protocol.

#### Scenario: A server that speaks the newer revision is adopted

- **WHEN** a connection is opened to a server that answers the discovery probe with a revision the
  client knows
- **THEN** the session runs on that revision
- **AND** the connection holds no server-assigned session identifier

#### Scenario: A server that does not know the probe keeps the handshake

- **WHEN** a connection is opened to a server that answers the discovery probe with "method not
  found"
- **THEN** the session completes the handshake instead
- **AND** the tools it lists, the calls it serves and the errors it returns are those the handshake
  era produced before this capability existed

#### Scenario: The era is decided again after a reconnect

- **WHEN** a connection drops and is re-established against a server whose support has changed
- **THEN** the new session runs on the era that server now answers with
- **AND** no state carried over from the previous session decides it

### Requirement: A server that does not speak the newer revision loses nothing

Adopting the newer revision SHALL NOT change what a connection to a handshake-era server does.
Its protocol version, its tool listing, its call results, its keepalive and its reconnect behaviour
SHALL be exactly what they were before the negotiation existed.

The machinery that keeps a session alive — supervision, the reconnect ladder, the dead-session
verdict, the in-flight count — SHALL remain unconditional rather than being gated on the era. A
connection can land in either era at any reconnect, and losing the transport is not a property of
either one.

That machinery SHALL include a detector for a session that fails without raising, on every era. A
connection SHALL NOT depend on the idle keepalive for this, because the keepalive is suppressed on
the newer revision; nor SHALL it depend on a tool call failing, because that reports the drop to the
model as a tool error before anything reconnects.

**A session dies without raising in two shapes, and the detector SHALL cover both** — they are
reported differently and one mechanism does not reach the other. Where the client library reports a
transport-level fault **as data** rather than by raising it, the connection SHALL observe it and end
the serving cycle. Where the transport's inbound stream instead **ends carrying nothing** — the
library's receive loop leaving normally, marking itself closed and waking pending requests, without
reporting a fault by either route — the connection SHALL likewise observe the end and end the
serving cycle. A clean end is not an edge case: it is what an idle peer closing looks like, so a
gateway read timeout, a load balancer dropping a held stream and a server restart all arrive this
way. Covering only the first shape satisfies the letter of this requirement and none of its
purpose, because the session then stays live to every reader until a tool call fails against it.

The failure this detector answers exists only where a transport holds a stream open between
requests: on both eras for a transport whose stream is opened before negotiation and held for the
session, and on the handshake era for one whose stream belongs to that handshake. A connection that
holds nothing between requests has nothing that can die unobserved — its next request opens its own
channel and fails there — so the requirement is satisfied vacuously in that combination rather than
by a second mechanism. **The detector SHALL still be registered unconditionally**, because which
combination a connection is in is decided per session by what the server answers.

A fault the client attributes to the peer's message rather than to the transport — a frame it
cannot parse, which the library reports by the same route while the stream stays readable — SHALL
NOT end the session. Ending it would answer one malformed message with a reconnect, and a peer that
emits them steadily with one reconnect per message.

#### Scenario: The fallback path is exercised against a refusing server

- **WHEN** the test suite drives a connection against a server that refuses the discovery probe
- **THEN** the connection serves tools over the handshake era
- **AND** its keepalive, reconnect and dead-session handling behave as they do for any handshake-era
  server

#### Scenario: A dropped transport reconnects on either era

- **WHEN** the transport of a session drops
- **THEN** the connection reconnects by the same ladder whichever era that session had negotiated

#### Scenario: A transport that fails quietly is noticed on the era with no keepalive

- **WHEN** a session that negotiated the revision without a keepalive loses its transport in a way
  the client library reports as data rather than by raising
- **THEN** the connection ends that session and reconnects without waiting for a tool call
- **AND** no tool error reaches the model for a session that was already gone

#### Scenario: A stream that simply ends is noticed on the era with no keepalive

- **WHEN** the inbound stream of a session that negotiated the revision without a keepalive ends
  carrying nothing — no fault reported as data and none raised out of the session
- **THEN** the connection ends that session and reconnects without waiting for a tool call
- **AND** the connection stops advertising the session as live at the moment the stream ends,
  rather than when the next call is dispatched into it

#### Scenario: A message the client cannot parse is not read as a lost transport

- **WHEN** a peer sends a frame the client fails to parse, and the stream stays readable
- **THEN** the session continues and the connection does not reconnect
- **AND** the unparseable frame is reported

### Requirement: What the era removes is gated on the era, not on the transport

Behaviour the newer revision removes SHALL be suppressed by testing the **negotiated era**, never by
testing the transport name. Both remaining transports can carry either era: the discovery probe
travels a transport's ordinary request path, so a transport whose shape stays stateful still
negotiates the stateless envelope.

The idle keepalive SHALL NOT be sent on a connection that negotiated a revision without it. A
connection that negotiated such a revision SHALL hold its session by waiting rather than by polling.

#### Scenario: The keepalive is suppressed on every transport that negotiated the newer era

- **WHEN** a session on any transport has negotiated a revision from which the keepalive was removed
- **THEN** no keepalive request is sent for the life of that session
- **AND** the suppression is decided by the negotiated revision, not by which transport carries it

#### Scenario: The keepalive still runs on a handshake session

- **WHEN** a session negotiated the handshake era
- **THEN** its idle keepalive runs on the interval it ran on before

### Requirement: The negotiated era is on the record

A connection SHALL report the protocol revision it negotiated when it first adopts one and whenever
that revision changes. A server that quietly answers the older era is otherwise indistinguishable
from one that never received the probe, and the adoption becomes unfalsifiable after deployment.

The report SHALL NOT repeat at that level for a reconnect that negotiated the same revision again.
The reconnect ladder starts at one second, so a server that accepts a connection and drops it would
otherwise emit a record per attempt, and the record that carries information is the one that reports
a change.

#### Scenario: A session names its era at start-up

- **WHEN** a connection establishes its first session
- **THEN** one log record names the server and the protocol revision that session runs on

#### Scenario: A flapping server does not repeat its era

- **WHEN** a connection reconnects and negotiates the revision it already reported
- **THEN** no further record is emitted at the level the first one was

### Requirement: The header channel the newer era opens is bounded and pinned

The header channel the newer revision opens SHALL stay inside its own namespace. Where a server
annotates tool arguments so that the client mirrors their values into request headers, that channel
SHALL NOT set or overwrite the headers this system relies on for authorization or attribution.

The bound SHALL be asserted by a test rather than assumed from the client library's current
behaviour, so that a library change which widens the namespace fails here rather than in a
deployment.

The argument for accepting the channel is that the servers it reaches are operated by the same party
that declares the annotations. That argument SHALL be recorded where the server list is owned, so
that adding an externally operated server is a decision made against it rather than one made without
noticing it.

#### Scenario: A mirrored argument cannot displace an authorization or attribution header

- **WHEN** a tool call carries an argument the server annotated for mirroring
- **THEN** the outbound request carries it under the mirroring namespace
- **AND** the authorization, trace and agent headers the connection sends are unchanged

#### Scenario: A tool the client cannot honour is dropped visibly

- **WHEN** a server's listing declares an annotation the client refuses, and the tool is withheld
- **THEN** the change in that server's tool set is reported the way any other change to it is
