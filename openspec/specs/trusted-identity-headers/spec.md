# trusted-identity-headers Specification

## Purpose

How an instance may take the owner, the agent and the chat of an inbound message from a
deployment-trusted proxy instead of from its own configuration: the setting that declares the trust,
the fail-closed rule, minting once per connection, and the browser surface's use of it. The owner is
what the proxy authenticated; the agent and the chat are chosen by the caller and verified inside
that owner, so a header naming an agent the owner does not hold is refused rather than defaulted. Trust is a property of the
deployment topology and is never inferred from anything in the request. Scoped to the browser
surface today; the requirements are written so the other surfaces can adopt the same mechanism
without restating them.
## Requirements
### Requirement: Trust in a header is declared by the deployment, never by the sender

An instance SHALL take an identity axis from a request header only while the deployment's mode
declaration says that the listening port is fronted by a proxy that authenticates the caller and
overwrites those headers on whatever the caller sent. That declaration SHALL be the same one that
governs every other multi-owner behaviour, not a setting of its own: resolving real owners and
storing data per owner are one decision, and two settings expressing it can disagree.

While the declaration is off the **owner** SHALL come from configuration and SHALL NOT be read from a
header: that is what lets a client of this mode send no identity headers at all, and what keeps the
registry surfaces beside it answering for the same owner. The agent and the chat MAY be named by the
request, with the configured agent and the transport's own convention as the answer for an absent
one, because with one owner a header can only select within that owner and a caller who reaches this
port already holds everything it has.

The trust SHALL be a property of the topology and SHALL NOT be inferred from anything in the request:
no token in the header, no source address, and no absence of a header SHALL be treated as evidence
that a caller is or is not the proxy. Documentation of the mode SHALL state that enabling it on a
directly reachable port lets any caller claim any owner.

#### Scenario: The default is the constant owner

- **WHEN** the mode is absent or off and the request names neither an agent nor a chat
- **THEN** every scope the surface builds carries the configured owner and the surface's conventional
  chat
- **AND** no owner header is read for identity on any code path

#### Scenario: One declaration governs both identity and storage

- **WHEN** the settings surface is inspected
- **THEN** no setting declares trusted headers separately from the mode

#### Scenario: Trust is not derived from the request

- **WHEN** the resolution path is inspected
- **THEN** nothing in it reads a source address, a bearer token, or a header signature to decide
  whether the headers may be trusted
- **AND** the only input to that decision is the mode declaration

### Requirement: Header resolution is fail-closed

While the setting is on, an inbound connection or request on the covered surface SHALL be refused
when the owner header or the chat header is absent, empty, or carries a value the rule below
refuses. The refusal SHALL be the transport's own immediate answer — a closed connection for a
stream, an error response for a request — and SHALL name the reason.

No path SHALL fall back to the configured owner while the setting is on. A fallback would restore the
shared bucket this capability exists to end, and would do it silently, on exactly the deployment that
asked for separation.

The same rule SHALL hold for the agent axis: while the setting is on, a registry that cannot be
reached or cannot answer SHALL refuse the connection or request rather than falling back to the
configured agent. The failure this prevents is quieter than the owner's and worse to unpick — a
served request whose scope names an agent the record does not hold writes rows under a key nothing
will look them up by.

The chat header SHALL be required to carry a non-empty value that the surface's chat-id policy does
not call synthetic; a value the policy does call synthetic SHALL be refused rather than clamped or
remapped, because the synthetic values select bridge and agent-to-agent routing and a caller aiming
at one must not land inside a real conversation. Beyond that refusal exactly one bound SHALL be imposed and no
other shape: a length cap, because the value is a key column in two btree indexes and PostgreSQL
refuses an index tuple over its page limit — an unbounded id is a write that fails at the storage
layer rather than at the boundary that admitted it. In particular the value SHALL NOT be required to
be a number.
A conversation a client names is a storage key this instance did not choose, exactly as the owner is;
requiring it to be a number the server already knows how to count to is what leaves a client unable
to name a conversation that does not yet exist.

The admitted value SHALL be treated as opaque: surrounding whitespace SHALL be stripped and nothing
else SHALL be normalized — no numeric folding, no case folding, no canonical form. Two spellings are
therefore two conversations. Normalizing would be the same defect as clamping, one step later: a
client that asks for one conversation would be served another's history, and the two spellings would
be indistinguishable in every stored row afterwards.

#### Scenario: A missing owner header refuses the connection

- **WHEN** the setting is on and a client connects without the owner header
- **THEN** the connection is refused with a stated reason
- **AND** no scope is built and no message is accepted

#### Scenario: A chat id the policy calls synthetic is refused

- **WHEN** the setting is on and the chat header carries a value the chat-id policy calls synthetic
- **THEN** the connection is refused
- **AND** the value is not clamped, remapped, or replaced by the surface's conventional chat

#### Scenario: A chat id longer than the cap is refused at the boundary

- **WHEN** the setting is on and the chat header carries a value longer than the cap
- **THEN** the connection is refused with a stated reason naming the cap
- **AND** the refusal happens before any row is keyed by that value

#### Scenario: An absent or empty chat header is still refused

- **WHEN** the setting is on and the chat header is absent, empty, or holds only whitespace
- **THEN** the connection or request is refused with a stated reason
- **AND** no scope is built and no message is accepted

#### Scenario: A chat id that is not a number is served

- **WHEN** the setting is on and the chat header carries a non-empty value that no numeric reading
  applies to
- **THEN** identity resolves and the minted scope's chat axis is that value with its surrounding
  whitespace stripped and nothing else changed
- **AND** it is neither refused for its shape nor replaced by the surface's conventional chat

#### Scenario: Two spellings of one number are two conversations

- **WHEN** the setting is on and two requests of one owner present chat headers `7` and `07`
- **THEN** they mint two scopes whose chat axes differ
- **AND** neither request reads the rows written under the other

#### Scenario: An unreachable registry refuses the connection

- **WHEN** the setting is on and the registry cannot answer while a scope is being minted
- **THEN** the connection or request is refused with a stated reason
- **AND** no scope is built carrying the configured agent

#### Scenario: No fallback exists while trust is declared

- **WHEN** the resolution path is inspected with the setting on
- **THEN** every branch either produces a scope built from the headers and the registry, or refuses
- **AND** none produces a scope carrying the configured owner or the configured agent

### Requirement: A stream's identity is minted once and never re-read from a frame

On a surface that holds a long-lived connection, the scope SHALL be built once, at the handshake,
from the headers of that handshake, and SHALL be held for the life of the connection. Every frame
arriving on that connection SHALL be attributed to that scope.

No frame body SHALL be consulted for any of the four axes, on any path, whether the setting is on or
off. This is the invariant the surrounding capability already holds — a sender cannot name its own
owner — and moving the trusted fact from configuration to the deployment boundary SHALL NOT weaken
it. A per-frame owner would let one connection speak for several owners, which is the same defect as
reading it from the payload.

#### Scenario: Two connections under different owners stay separate

- **WHEN** two connections are opened with different owner headers and each sends a message
- **THEN** each message is attributed to the owner its own connection presented

#### Scenario: A frame cannot change the connection's identity

- **WHEN** a frame carries an owner, agent, surface, or chat field in its body
- **THEN** the field is ignored for identity and the connection's minted scope is used

### Requirement: The browser surface resolves identity from headers

The browser surface SHALL implement this capability on its WebSocket handshake and on **every route
that reads or writes owner-bearing state**: the chat history route, the runtime-state route, the
attachment download route, the attachment upload route and the attachment delete route. All SHALL
read the same two headers and apply the same fail-closed rule, so that a client which may connect can
also read exactly the history, the runtime figures and the files it is allowed to read.

A route SHALL read the headers per request, because it holds no connection to mint on. Its refusal
SHALL be a status response rather than a closed socket.

A route that reports figures belonging to an owner SHALL resolve identity before reading them, and
SHALL NOT serve an unauthenticated caller a figure that names or aggregates any owner. The health
route SHALL remain free of identity resolution, so that a deployment keeps one probe surface that
carries no owner's data.

A route that touches owner-bearing state SHALL NOT be mounted without resolving identity, and the
route collection SHALL be checked as a whole rather than route by route, so that adding a route
without resolution fails a test instead of shipping. Routes that expose no owner-bearing state — the
health surface — are exempt and SHALL be named as exempt.

Other surfaces SHALL keep the configured owner until they adopt this capability; adopting it SHALL
NOT require restating these requirements.

#### Scenario: History is scoped to the presented owner

- **WHEN** the setting is on and two clients request chat history with different owner headers
- **THEN** each receives only messages written under its own owner
- **AND** neither sees a message written under the other's

#### Scenario: The history route refuses the same inputs the handshake refuses

- **WHEN** the setting is on and a history request arrives without a valid owner or chat header
- **THEN** it is refused with a status response and no rows are read

#### Scenario: Runtime state is scoped to the presented owner

- **WHEN** the setting is on and two clients request runtime state with different owner headers
- **THEN** each receives the queue counts and spend of its own owner

#### Scenario: The runtime-state route refuses the same inputs the history route refuses

- **WHEN** the setting is on and a runtime-state request arrives without a valid owner or chat header
- **THEN** it is refused with a status response and no owner-bearing figure is read

#### Scenario: The file routes refuse the same inputs the handshake refuses

- **WHEN** the setting is on and a download, upload or delete request arrives without a valid owner
  or chat header
- **THEN** it is refused with a status response naming the reason
- **AND** no object is read, written or deleted

#### Scenario: The health route stays the unauthenticated probe

- **WHEN** the setting is on and the health route is requested with no headers
- **THEN** it answers as before and carries no owner's data

#### Scenario: No owner-bearing route is mounted without resolution

- **WHEN** the browser route collection is inspected
- **THEN** every route that reads or writes owner-bearing state resolves identity through the
  installed resolver
- **AND** the only routes that do not are the ones named exempt

### Requirement: Fan-out with no owner is disabled while identities are separate

While the setting is on, the covered surface SHALL NOT install a process-wide sink or subscription
that pushes content to every open connection. Runtime log lines and extension lifecycle events carry
no owner, so under several owners there is no correct connection to route them to, and delivering
them to all of them publishes one owner's content to another.

Disabling SHALL be the behaviour, not a warning: a deployment that has declared its identities
separate SHALL NOT have to also know which diagnostic channel leaks.

#### Scenario: Runtime logs do not reach connections while trust is on

- **WHEN** the setting is on and a runtime log line is emitted during one owner's task
- **THEN** no connection receives it
- **AND** with the setting off, the existing behaviour is unchanged

### Requirement: The identity resolver is installed by a composition root, never defaulted into existence

The holder of the process's identity resolver SHALL have no usable default. Reading it before a
composition root has installed one SHALL raise, and a process that mounts an owner-facing surface
SHALL refuse to start unless the installation happened. Installing the constant-owner resolver
SHALL be an explicit act with the mode off, exactly as installing the header-reading one is with the
mode on.

A default that resolves is a fail-open sanitizer wearing a different name. The failure it produces
is silent and total: every connection is served under the configured owner, so all of them share one
history and one conversation, and outbound matching on the owner axis matches every socket against
every message. The same default also declares that output carrying no owner may be fanned out, which
switches the runtime-log sink and the extension-lifecycle subscription back on, so one owner's
diagnostics reach every connected client. None of that is reported: there is no error, no log line
and no refused handshake, and the deployment looks healthy while it is a single shared bucket.

A second entrypoint is what makes this reachable. The installation lives in one lifespan today, so
any new composition root that forgets it inherits the default rather than a failure.

#### Scenario: A root that installs no resolver does not serve

- **WHEN** an entrypoint mounts an owner-facing surface without installing a resolver
- **THEN** startup fails naming the missing installation
- **AND** no connection is accepted and no request is served

#### Scenario: Reading the holder before installation raises

- **WHEN** the resolver holder is read before a composition root has installed one
- **THEN** the read raises
- **AND** it does not answer with a resolver that mints the configured owner

#### Scenario: The constant owner is installed explicitly

- **WHEN** the mode is off and the entrypoint composes its surface
- **THEN** it installs the constant-owner resolver explicitly
- **AND** every scope the surface builds afterwards carries the configured owner, and the surface's
  conventional chat for a request that names none

#### Scenario: Unattributed fan-out follows the installed resolver

- **WHEN** a process serving several owners has started
- **THEN** no process-wide log sink or lifecycle subscription that pushes to every connection is
  installed, because the resolver that permits it was never installed

### Requirement: The agent axis is selected at the boundary and resolved within the authenticated owner

The agent axis SHALL be resolved from the agent registry, for the owner the boundary settled, and
SHALL NOT come from configuration.

The boundary SHALL read an agent header. A request that carries none SHALL be served the owner's core
agent, which is the answer every client received while no header was read at all. A request that
carries one SHALL be served that agent **only if the registry holds it for the owner already
settled**, and SHALL be refused otherwise.

The resolution SHALL be a lookup whose predicate names the owner together with the agent. This is
what keeps the header safe, and it is the whole of the argument: a value that does not resolve
inside the settled owner names nothing, so the header selects among that owner's agents and cannot
reach past them. The axis is therefore one the caller *selects* and the deployment *verifies*, not
one the caller asserts — the property the previous prohibition existed to hold, kept by the lookup
instead of by refusing to look.

The asymmetry with the owner axis remains and is why the order of resolution is fixed: the owner
header decides *whose* records are reachable and is trustworthy only because a proxy authenticated
it, so the owner SHALL be settled before the agent is looked up, and never the other way around.

An agent header naming a record the registry does not hold for that owner SHALL be refused. It SHALL
NOT fall back to the core agent: a request that named a conversation's agent and is served a
different agent's conversation shows one conversation under another's name, which is worse than
being told no.

An agent header naming a record the registry holds as deactivated SHALL be refused on the same
terms. Deactivation is how an owner retires an agent; a retired agent that still accepts messages
is the defect the retired-conversation refusal already prevents one axis below.

The record resolved for an absent header SHALL be verified active on the same terms as a named one.
The reserved id is the answer an absent header carries, not an agent the boundary keeps alive: it is
deactivated by the same route as any other, and a boundary that served it regardless would honour a
retirement for every agent except the one no client has to name.

Resolution SHALL create the owner's core agent when none exists **and no agent header was
presented**, so an owner appearing for the first time is served without an administrative step. A
request that names an agent SHALL NOT create anything: the record it names either exists or the
request is refused.

Every header SHALL be validated before the registry is written, so a request refused for any of them
leaves no freshly created core agent behind.

Because the registry is consulted per request and per handshake rather than remembered, the scope a
surface mints SHALL reflect the record at the moment it mints it.

Exactly one spelling of the agent header SHALL be honoured. A second spelling accepted "for
convenience" is a second door onto the same decision, and the two drift.

#### Scenario: No agent header is the core agent

- **WHEN** the declaration is on and a request presents an owner header and no agent header
- **THEN** the scope built for it carries the core agent the registry holds for that owner

#### Scenario: A first-time owner is served

- **WHEN** the declaration is on and a request presents an owner header for an owner with no record,
  and no agent header
- **THEN** a core agent is created for that owner and the scope carries it
- **AND** the request is served rather than refused

#### Scenario: An agent header the owner holds is honoured

- **WHEN** the declaration is on and a request presents an owner header and an agent header naming an
  active agent the registry holds for that owner
- **THEN** the scope built for it carries that agent
- **AND** it does not carry the owner's core agent

#### Scenario: An agent header the owner does not hold is refused

- **WHEN** the declaration is on and a request presents an agent header naming a record the registry
  does not hold for the authenticated owner
- **THEN** the request is refused
- **AND** no scope is built carrying the core agent

#### Scenario: An agent header naming a deactivated agent is refused

- **WHEN** the declaration is on and a request presents an agent header naming an agent the registry
  holds for that owner with its active flag false
- **THEN** the request is refused

#### Scenario: A deactivated core agent is refused for an absent header

- **WHEN** the declaration is on and a request presents no agent header, for an owner whose record on
  the reserved id the registry holds with its active flag false
- **THEN** the request is refused
- **AND** no scope is built carrying that agent

#### Scenario: A request naming an agent creates nothing

- **WHEN** the declaration is on and a request presents an owner header for an owner with no record,
  together with an agent header
- **THEN** the request is refused
- **AND** no agent record is created for that owner

#### Scenario: Only one spelling of the agent header is read

- **WHEN** a request carries the agent header under a second spelling and not the declared one
- **THEN** that value is not read for identity
- **AND** the scope carries the owner's core agent

### Requirement: The configured-owner boundary selects the agent and the conversation it is told

Where the declaration is off, the owner SHALL remain the configured constant and SHALL NOT be read
from a header — that is what lets a client of this mode send no identity headers at all and lets the
registry surfaces beside it answer for the same owner.

The agent and the conversation SHALL be read from their headers on the same terms as above, with the
configured agent and the surface's conventional conversation as the answer for an absent one.

The safety this costs is none: the mode serves one owner, so a header can select only within that
owner, and a caller who reaches this port already holds everything the owner has. The safety the
other mode buys with a proxy is bought here by there being nothing else to reach.

#### Scenario: The configured owner is not read from a header

- **WHEN** the declaration is off and a request presents an owner header
- **THEN** the header is not read for identity
- **AND** the scope carries the configured owner

#### Scenario: An agent header is honoured under the configured owner

- **WHEN** the declaration is off and a request presents an agent header naming an active agent the
  registry holds for the configured owner
- **THEN** the scope carries that agent

#### Scenario: A conversation header is honoured under the configured owner

- **WHEN** the declaration is off and a request presents a chat header that names no synthetic
  conversation
- **THEN** the scope carries that conversation

#### Scenario: An absent conversation header is the surface's conventional conversation

- **WHEN** the declaration is off and a request presents no chat header
- **THEN** the scope carries the surface's conventional conversation
