# owner-scope Specification

## Purpose

One frozen identity value — `OwnerScope` — answers "whose data, which agent, on which surface, in
which chat" for every row the system writes and every task it runs. The value is minted exactly
once at the transport boundary from trusted facts and carried unchanged to the agent's run state;
no internal layer re-derives or extends it. It replaces `conversation_id`/`session_id` as
agent-facing identity immediately.

## Requirements

### Requirement: OwnerScope is the single identity value

`OwnerScope` SHALL be a frozen value with exactly four axes, in this order: `owner_id`,
`agent_id`, `surface_id`, and `chat_id` — the hierarchy reads owner -> agent -> surface -> chat.
The agent axis exists because one owner may run several agents; today one instance runs exactly
one, so the value is a constant, and the shape is fixed now so that the deferred storage
migration — which is keyed by the scope — never has to run a second time to gain the axis.
The type SHALL live at engine level (`new_core`), so `new_core/loop` can import it
directly without importing `ouroboros`. The closed surface dictionary and the scope type SHALL be
the one source of truth; transports and the agent SHALL import that type rather than redefining
identity fields.

#### Scenario: The identity type is engine-level and importable everywhere

- **WHEN** the `new_core` package is inspected
- **THEN** it contains the `OwnerScope` type and the closed `SurfaceId` dictionary
- **AND** neither `new_core/loop` nor `new_core/tools` imports an `ouroboros` module to name them

### Requirement: Scope construction is fail-closed

Every axis of `OwnerScope` SHALL be required, non-empty, and validated on construction. An
unknown `surface_id` SHALL be rejected, and extra fields SHALL be rejected. A scope SHALL NOT be
constructible with an empty owner, agent, surface, or chat.

#### Scenario: An empty axis is rejected

- **WHEN** an `OwnerScope` is constructed with an empty `owner_id`, `agent_id`, `surface_id`, or
  `chat_id`
- **THEN** construction raises

#### Scenario: An unknown surface is rejected

- **WHEN** an `OwnerScope` is constructed with a `surface_id` outside the closed dictionary
- **THEN** construction raises

#### Scenario: An extra field is rejected

- **WHEN** an `OwnerScope` payload carries a field outside the four axes (including
  `trace_id` or `conversation_id`)
- **THEN** construction raises

### Requirement: The surface dictionary is closed and equals the transport names

The `SurfaceId` dictionary SHALL be a closed set of the four transport surfaces (`dialogs`,
`browser`, `a2a`, `skill-bridge`). The set of transport `TRANSPORT_NAME` constants SHALL equal the
set of dictionary values, enforced by a test. Legacy spellings (`sberchat`, `web`, `internal`,
`unknown`) SHALL be accepted only when reading stored values, SHALL never be produced, and SHALL
be normalized to the closed values.

#### Scenario: Transport names equal the surface dictionary

- **WHEN** every transport's `TRANSPORT_NAME` is collected and compared with the `SurfaceId`
  dictionary
- **THEN** the two sets are equal

#### Scenario: Legacy spellings normalize on read only

- **WHEN** a stored scope is read with surface `sberchat`, `web`, `internal`, or `unknown`
- **THEN** it is accepted and normalized to the matching closed value
- **AND** no code path produces one of the legacy spellings

### Requirement: Scope round-trips through storage

`OwnerScope` SHALL serialize all four axes to a plain dict (`to_dict`) and deserialize strictly
(`from_dict`), so a scope written into a stored row or a task payload reads back as the identical
scope. `trace_id` SHALL NOT appear in the serialized form: it is observability, not identity.

#### Scenario: A stored scope reads back unchanged

- **WHEN** a scope is serialized, written into a row, read back, and deserialized
- **THEN** the result equals the original scope, with all four axes intact

#### Scenario: Strict read refuses a malformed scope

- **WHEN** `from_dict` reads a payload missing any of the four axes or carrying an unknown surface
- **THEN** it raises rather than substituting a default

### Requirement: Scope is minted at the transport boundary

The scope a task runs under SHALL be constructed by the transport itself, at its own boundary.
There SHALL be no shared resolver: a transport already knows its surface, and a helper that takes
one as an argument is a helper any caller can be handed.

No axis SHALL be read from the message. The surface SHALL be the transport's own `TRANSPORT_NAME`,
narrowed to the closed dictionary at the point of construction; the chat SHALL be the conversation
the message belongs to. A message body or envelope metadata SHALL reach none of the four, because
the code holding them is holding attacker-controlled data.

The owner and the agent SHALL come from the instance's configured single owner and single agent,
with one exception: on a surface whose deployment has declared its port fronted by an authenticating
proxy, the owner and the chat MAY be read from named request headers of the connection or request
itself, and the agent SHALL be resolved from the agent registry for the owner that boundary
authenticated. The declaration SHALL be configuration, never anything in the request. The distinction
is what the header is: a header a peer can set is a message body under another name, while a header a
trusted proxy writes after authenticating the caller — and overwrites on whatever the caller sent —
is a fact of the boundary, the same category as configuration. Absent that declaration the
prohibition on headers stands unchanged and the agent axis comes from configuration.

The agent axis MAY be named by a header, and SHALL be resolved by a lookup whose predicate carries
the owner that boundary settled. The lookup is what keeps it a fact the deployment verifies rather
than one a caller asserts: the owner is authenticated by the proxy, and the agent is then looked up
*within* that already-resolved owner, so nothing a caller sends can widen which owner's agents are
reachable — a value that resolves to nothing inside that owner is refused rather than carried. An
absent header SHALL resolve to the owner's core agent, which is the axis every client received while
no header was read.

A surface reading headers under that declaration SHALL fail closed: an absent, empty or unparseable
header SHALL refuse the connection or request rather than falling back to the configured owner. A
registry that cannot answer SHALL refuse on the same terms rather than falling back to the configured
agent. A fallback would put two owners back in one bucket precisely on the deployment that asked to
separate them, and a guessed agent would key an owner's rows under an agent the record does not have.

Where the transport is reading a row it already stored, it SHALL read the ownership pair back off
that row rather than constructing a fresh one — the row records the owner and agent it was written
under, and only the surface is absent from it.

Construction SHALL be confined to the transports and SHALL be enforced by a test that inspects
every construction site in the system. Residing under the transport package is not sufficient on its
own: a sweep, an agent card and a boot check are not transports handling their own rows, and they
SHALL name themselves with a service identity instead. Work that has an author SHALL carry that
author's scope, however far from the boundary it runs and however long after, and SHALL NOT
reconstruct one from an adjacent field.

No default SHALL be applied to a surface at construction. A value that names no surface SHALL fail
rather than resolving to one, so that a delivery source, a normalized task source, or an empty string
cannot become an identity axis by absorption. Leniency SHALL exist only in the read direction, when a
stored scope is parsed.

#### Scenario: No construction names an owner or an agent of its own

- **WHEN** every `OwnerScope` construction in the system is inspected
- **THEN** its owner and agent come from the instance's configuration, from the stored row being read
  back, or — where the deployment declares the port trusted — the owner from that boundary's named
  header and the agent from the registry lookup for that owner, and from nowhere else
- **AND** no construction takes the agent axis from a header value directly: the header may only be
  an argument to that lookup

#### Scenario: A built scope carries the configured owner and agent

- **WHEN** a transport builds a scope for an inbound message and no trusted-header declaration is in
  force
- **THEN** `owner_id` equals the instance's configured single owner, `agent_id` equals the
  instance's configured single agent, and `surface_id` equals the transport's `TRANSPORT_NAME`

#### Scenario: A declared boundary supplies the owner and the chat

- **WHEN** a transport builds a scope on a surface whose deployment declares its port trusted and the
  request names no agent
- **THEN** `owner_id` and `chat_id` come from that boundary's named headers, `agent_id` is the core
  agent the registry holds for that owner, and `surface_id` is still the transport's `TRANSPORT_NAME`
- **AND** a missing or malformed header refuses rather than producing a scope

#### Scenario: No header names the agent

- **WHEN** a request under a declared trusted boundary carries no header naming an agent
- **THEN** the agent axis is the core agent the registry holds for the authenticated owner

#### Scenario: A header naming the agent is resolved, not trusted

- **WHEN** a request under a declared trusted boundary carries a header naming an agent
- **THEN** the agent axis is the record the registry holds for that name **under the authenticated
  owner**
- **AND** a name the registry does not hold for that owner refuses rather than producing a scope

#### Scenario: An unanswerable registry refuses rather than defaulting

- **WHEN** a transport builds a scope under a declared trusted boundary and the registry cannot be
  reached
- **THEN** the connection or request is refused naming the reason
- **AND** no scope is produced carrying the configured agent

#### Scenario: Only a transport builds a scope

- **WHEN** every `OwnerScope` construction is inspected
- **THEN** each lies in a transport — either an ingress path handling a message arriving from
  outside, or a reader of rows that transport itself stored
- **AND** no module of the supervisor, the agent, or the storage layer constructs one

#### Scenario: An unnameable surface fails rather than defaulting

- **WHEN** a value that names no surface reaches a construction site
- **THEN** the construction fails
- **AND** no lenient helper converts it to a surface first

### Requirement: Run state carries a required scope

`RunConfig.scope` and `RunState.scope` SHALL be required fields of type `OwnerScope`; a run state
SHALL never hold a `None` scope. `make_run_state` SHALL copy the scope from the run configuration.

#### Scenario: A run state always has a scope

- **WHEN** a `RunState` is constructed through `make_run_state`
- **THEN** `state.scope` is the `OwnerScope` from the run configuration
- **AND** the field type is `OwnerScope`, not `Optional`

### Requirement: ToolContext carries a required scope and tools address through it

`ToolContext.scope` SHALL be a required field of type `OwnerScope`. The projection fields
`current_chat_id`, `current_task_source`, `memory_owner_id`, and `conversation_id` SHALL be
removed from `ToolContext`. Tool bodies SHALL address owner, surface, and chat strictly through
`ctx.scope`. The same value carries `agent_id`; no tool addresses through it today.

#### Scenario: A ToolContext requires a scope

- **WHEN** a `ToolContext` is constructed without a scope
- **THEN** construction raises

#### Scenario: Tools address through scope only

- **WHEN** tool bodies that send messages, write memory, or route events are inspected
- **THEN** none reads `current_chat_id`, `current_task_source`, `memory_owner_id`, or
  `conversation_id`, and each addresses through `ctx.scope.owner_id` / `ctx.scope.surface_id` /
  `ctx.scope.chat_id`

### Requirement: conversation_id and session_id are not agent-facing

`conversation_id` SHALL NOT exist anywhere: not in the task dict handed to the agent, not in agent
events, not in a runtime model, not in a transport contract, and not as a stored column. The scope's
axes SHALL be what stored rows are keyed by, and the supervisor SHALL NOT derive a conversation key
at its boundary. `session_id` SHALL remain the runtime epoch — never an ownership axis — and SHALL
NOT appear in the task dict handed to the agent, SHALL NOT be read by the agent, and SHALL NOT be
emitted in agent-produced events.

#### Scenario: The agent input carries no conversation identity keys

- **WHEN** the task dict a worker passes to `handle_task` is inspected
- **THEN** it carries `scope` and neither `conversation_id` nor `session_id`

#### Scenario: Agent events carry no conversation identity keys

- **WHEN** agent-emitted events (`send_message`, `typing_stop`, orchestration events) are inspected
- **THEN** none carries `conversation_id` or `session_id`

#### Scenario: No layer derives a conversation key

- **WHEN** the supervisor, a transport, or the agent writes a stored row
- **THEN** it supplies the scope, and no code glues a surface and a chat into one key

### Requirement: Background memory carries the scope of whoever caused the work

Background memory SHALL NOT mint a scope. Every event it consumes SHALL carry the `OwnerScope` of the
action that produced it, and every store read, every store write, and every `ToolContext` a handler
builds SHALL address that scope. No module of the background memory package SHALL import a scope
mint.

Which axes then partition the work is the store's business and is unchanged by this: memory and the
committed summary are keyed by the ownership pair, so the surface and the chat travel with the scope
without narrowing anything.

#### Scenario: The handlers mint nothing

- **WHEN** the background memory package's modules are inspected
- **THEN** none imports or calls a scope mint

#### Scenario: A distillation run executes under its event's scope

- **WHEN** the distillation handler builds its tool context for `memory_save`
- **THEN** the context's scope is the one the event carried, not a minted one

#### Scenario: A consolidation run reads and commits under its trigger's scope

- **WHEN** a run reads the backlog and commits a summary
- **THEN** both address the ownership axes of the scope its trigger carried

### Requirement: Author-less work names itself with its own type

Work that no user caused — a boot check, an agent card, a journal sweep, a protocol row — SHALL
identify itself with a `ServiceScope`, which SHALL NOT be an `OwnerScope` and SHALL NOT be a subtype
of one. It SHALL carry the instance's ownership pair and a member of a closed dictionary naming the
job, and SHALL carry neither a surface nor a chat, because it has neither.

The job dictionary SHALL be closed for the same reason the surface dictionary is: the set of
author-less jobs is finite and known, and spelling it as a free string is what allowed an author-less
identity to be mistaken for a conversation.

A `ServiceScope` SHALL NOT be accepted where an `OwnerScope` is required. In particular it SHALL NOT
reach any announcement that causes background memory work, so that a system write cannot enqueue
consolidation over a real owner's backlog.

Where a storage call does require an `OwnerScope`, the service identity SHALL widen to one **only
through an explicit, named conversion**, never by subtyping or implicit coercion. The widened value
SHALL keep the instance's ownership pair and SHALL name the job where a conversation would go,
because there is none. The conversion SHALL run one way: no `OwnerScope` becomes a `ServiceScope`,
so a call site that widens is visible as the place the decision was made.

The widening SHALL NOT promise that its surface and chat axes go unread —
`new_core/identity/service_scope.py::as_scope` states that both are placeholders that match no
stored row, and that a `messages` or `context_summaries` read under one matches nothing, silently.
Messages and `context_summaries` are keyed by the chat, so a type that merely asserted "nothing reads
either" would be asserting a fact it cannot hold. A type SHALL NOT assert a fact it cannot hold. What
the widening SHALL state instead is what its chat axis *is*: the job's name, standing where a
conversation would.

That name is not a conversation any transport ever wrote, so it SHALL NOT be used as a chat
predicate. No widening site SHALL reach a statement that filters on the chat axis, and a test that
inspects every widening site SHALL enforce it. The failure this forbids is silent rather than loud:
such a read matches zero rows for every owner, forever, and reports exactly what a correct read that
found nothing reports — which is how the journal sweep's "resubmitted 0" would stay healthy while no
crashed message is replayed again.

Author-less work that must reach a chat-keyed shape SHALL read across chats explicitly, through a
read surface that names no chat, rather than by handing that surface a scope whose chat axis it
would then filter on. `transport/journal.py::unprocessed_records` is the single site that previously
widened to reach a `messages` statement, and it no longer widens at all: it takes the owner and the
agent and calls the store's pair-wide reader, so there is no chat axis for it to get wrong. After
that, **no** widening site reaches a `messages` or `context_summaries` statement — every survivor
addresses owner runtime state, a JSON document, a runtime event, skill state, an attachment or the
A2A task store, none of which is keyed by chat — and a test SHALL enforce that, over every
production root including the composition roots under `multiuser_config/`.

#### Scenario: A service identity is not an owner scope

- **WHEN** a `ServiceScope` is passed where an `OwnerScope` is required
- **THEN** it is rejected as a type error rather than silently accepted

#### Scenario: The job names a job, not a chat

- **WHEN** a `ServiceScope` is inspected
- **THEN** it names its job from a closed dictionary
- **AND** it carries no surface and no chat id

#### Scenario: System writes cannot trigger owner-scoped background work

- **WHEN** an author-less write reaches the outbound persistence path
- **THEN** it cannot publish a chat-message-persisted announcement, because that announcement
  requires an `OwnerScope`

#### Scenario: A sweep over an owner's rows names the pair rather than widening

- **WHEN** a background sweep reads every row belonging to an owner and agent
- **THEN** it passes the owner and the agent to a read surface that takes no scope
- **AND** it builds no widened scope at all, so there is no job name for a predicate to meet

#### Scenario: A widened job name never becomes a chat predicate

- **WHEN** every site that widens a service identity into an `OwnerScope` is inspected
- **THEN** none of them reaches a statement that filters on the chat axis
- **AND** the widening's own contract states that the chat axis holds the job's name, rather than
  claiming that nothing reads it

#### Scenario: The crash-replay sweep still finds rows written in real chats

- **WHEN** one owner has unprocessed journal rows in two different chats and the sweep runs
- **THEN** both rows are resubmitted
- **AND** the sweep reports two, not a healthy zero

### Requirement: The owner's chat carries a stored scope

The owner's chat SHALL have a persisted `OwnerScope`, recorded at the point ownership is claimed,
where the claiming message's scope is in hand. Work addressed to the owner rather than to a task's
conversation — a timeout notice — SHALL carry that stored scope.

The stored chat id and the stored chat source SHALL be derived from it rather than held as
independent fields, so that no path can resolve a source to a sentinel string and have that sentinel
absorbed into a surface.

#### Scenario: Ownership is claimed with a scope

- **WHEN** an instance records who its owner is from an inbound message
- **THEN** the message's scope is stored alongside

#### Scenario: An owner-addressed notice carries rather than mints

- **WHEN** a restart or timeout notice is sent to the owner
- **THEN** it addresses the owner's stored scope
- **AND** no scope is minted on that path

### Requirement: The scope derives its security token

`OwnerScope` SHALL expose `security_token`, a read-only value derived from the scope's own `owner_id`,
`agent_id` and `surface_id` and from nothing else — never from `chat_id`. It SHALL be the hexadecimal
form of a UUID version 5 in the URL namespace over the compact JSON array `[owner_id, agent_id,
surface_id]`, computed from the axes after construction has normalized them, so a legacy surface
spelling and surrounding whitespace yield the token of the normalized scope.

The token SHALL NOT be a field of the scope: it SHALL NOT be accepted as input by construction or by
`from_dict`, SHALL NOT appear in `to_dict`, and SHALL NOT be rendered by the scope's `repr` or `str`. The
scope's four axes, its equality, its hash and its serialized form SHALL be unchanged by it, so no stored
row, task payload, stream record or schedule fingerprint changes.

The token SHALL NOT be treated as a secret or as a credential: anyone who knows the three ids can compute
it. No boundary SHALL accept a security token from a request header, a request body, a frame or an
endpoint parameter; every consumer SHALL read it off a scope that was minted by the rules of this
capability.

#### Scenario: The token is the same across chats

- **WHEN** two scopes share owner, agent and surface and differ only in chat
- **THEN** their security tokens are equal

#### Scenario: The token differs by owner, by agent and by surface

- **WHEN** a scope's token is compared with that of a scope differing only in owner, only in agent, or
  only in surface
- **THEN** the tokens are not equal in each case

#### Scenario: Axis values containing a separator do not collide

- **WHEN** tokens are derived for owner `x:y` with agent `z` and for owner `x` with agent `y:z` on the same
  surface and chat
- **THEN** the two tokens are not equal

#### Scenario: The token is computed after normalization

- **WHEN** one scope is read with the legacy surface `web` and surrounding whitespace on its owner, and
  another is built with surface `browser` and the trimmed owner
- **THEN** their security tokens are equal

#### Scenario: The token is rejected as input

- **WHEN** an `OwnerScope` is constructed, or read with `from_dict`, from a payload carrying
  `security_token`
- **THEN** construction raises

#### Scenario: The token is neither serialized nor rendered

- **WHEN** a scope is serialized with `to_dict`, its model fields are listed, and it is rendered with
  `repr` and `str`
- **THEN** none of them contains `security_token` or the token's value
