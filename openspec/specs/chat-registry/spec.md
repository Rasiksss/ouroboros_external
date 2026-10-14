# chat-registry Specification

## Purpose

Which conversations an owner has for one of their agents. A durable record keyed on the owner
together with the agent and the chat, created by a conversation's first message as an upsert the
database decides, so a chat a client opens is listable without a seeding step. Each record carries a
title and the origin of that title: provisional from the opening of the first message, generated
later in the background memory tier, or set by the owner, which nothing overwrites. Retirement is a
flag flip and never a delete, because the chat axis keys rows that outlive the conversation, and
clearing a conversation's history is a different operation. The registry calls no model and names no
transport; it exposes a router that the single-process runtime and the config registry tier each
include under their own prefix, the third owner-bearing route group the invariants admit.
## Requirements
### Requirement: An owner's conversations are a record, keyed on the chat as well as the agent

The system SHALL keep a durable record of the conversations an owner holds with one of their agents.
Each record SHALL carry the owner, the agent, the chat id, a title, the origin of that title, how
many times a title has been attempted for it, whether it is active, and when it was created.

It SHALL NOT carry a last-activity value. Keeping one turns a row written once per conversation into
a row written once per message, on the inbound path, in exchange for an ordering the client can
compute from the history it already holds — and it puts a second writer on a row whose other writer
is the background tier's compare-and-swap. Recency ordering, if it is wanted, is an aggregate over
the message table inside the listing statement, not a hot-path write.

A chat id SHALL be unique within its owner and agent and SHALL NOT be assumed unique across either —
the same reading of the agent axis the agent registry already takes, one axis down. Every statement
the registry issues SHALL be keyed on the owner together with the agent and the chat, and a listing
on the owner together with the agent, written out inside the method that issues it. No query may
reach a conversation belonging to another owner or to another of that owner's agents, and the
isolation SHALL be in the predicate rather than in a check above it.

The record SHALL carry no surface axis. It describes exactly the rows `messages` and
`context_summaries` are keyed by once `key-chat-history-by-chat` lands, and those carry the owner,
the agent and the chat and nothing else; two surfaces that could mint one chat id under one owner and
agent therefore already share one history, which the storage key decides and no column here would
change. A surface column would describe an isolation the stored history does not have.

#### Scenario: A conversation belongs to exactly one owner

- **WHEN** two owners each hold a conversation recorded under the same chat id
- **THEN** each owner's listing contains its own conversation and not the other's
- **AND** a rename issued by one owner leaves the other's title unchanged

#### Scenario: Two agents of one owner keep separate conversations

- **WHEN** one owner holds conversations recorded under the same chat id for two different agents
- **THEN** each agent's listing returns its own conversation
- **AND** retiring one leaves the other active

#### Scenario: The isolation is in the predicate

- **WHEN** the statements the repository issues are inspected
- **THEN** each lookup, rename and retirement names the owner, the agent and the chat
- **AND** each listing names the owner and the agent
- **AND** none of them relies on a caller having checked ownership first

### Requirement: A conversation's record is created by its first message, as an upsert decided in the statement

The record SHALL come into existence when the conversation's first message is recorded, not by a
seeding step and not by an administrative action, so a conversation a client opens is listable
without anything else having run.

Creation SHALL be atomic against concurrent callers: an insert-if-absent whose conflict the database
resolves, followed by the read, in one transaction. A read followed by a conditional write SHALL NOT
be used — several transport instances share one table and a client may put several messages of a new
conversation in flight at once, so the first writes of a new chat race.

The conflicting insert SHALL write nothing at all. It SHALL NOT overwrite the title, or a second
message would undo a rename; it SHALL NOT set the active flag either, so a message arriving on a
retired conversation — an unsolicited answer from a task that was still running when the owner
retired it — leaves it retired rather than resurrecting it in the listing. A statement that only ever
writes on the insert is what keeps the inbound path free of a write conflict with the background
tier's title write.

The HTTP surface SHALL mint no conversation. It lists, renames and retires what the message path
recorded, so a request naming a chat with no record is a missing resource rather than a creation.

#### Scenario: A first message creates the record

- **WHEN** a conversation's first message is recorded
- **THEN** a record exists for that owner, agent and chat
- **AND** it appears in that owner's listing for that agent

#### Scenario: Concurrent first messages produce one record

- **WHEN** several instances record the first messages of one new conversation at the same moment
- **THEN** exactly one record exists for that owner, agent and chat afterwards
- **AND** every one of those callers receives it

#### Scenario: A later message disturbs nothing on the record

- **WHEN** a further message is recorded on a conversation whose title an owner has set
- **THEN** the stored title is unchanged
- **AND** no field of the record moved

#### Scenario: A message on a retired conversation does not resurrect it

- **WHEN** a message is recorded on a conversation the owner has retired
- **THEN** the record stays inactive
- **AND** it does not reappear in the listing

#### Scenario: The surface creates nothing

- **WHEN** a rename or a retirement names a chat that has no record
- **THEN** the request is refused as a missing resource
- **AND** no row is written

### Requirement: A title is provisional until it is generated, and a name the owner set is never overwritten

Each record SHALL carry a title together with the origin of that title: written provisionally by the
transport, generated in the background, or set by the owner.

The transport SHALL write a provisional title in the same statement that creates the record, taken
from the opening of the first message and bounded in length, so a listing renders something the
moment the conversation exists rather than after a model has run.

A generated title SHALL replace a provisional one and SHALL NOT replace one the owner set. That
condition SHALL be part of the update statement, matching on the stored origin, rather than a read
the caller performed earlier: the generation runs in another tier and lands after an unbounded delay,
during which the owner may have renamed the conversation. An owner's rename SHALL replace a title of
any origin and SHALL record the owner as the new origin, so nothing later overwrites it.

The registry SHALL call no model. A generated title arrives from the background memory tier as a
value to store; how that tier is reached, and what an instance too old to know the work does with it,
belong to `background-memory-events`. The number of attempts a conversation may cost SHALL be part of
the record, and the permission to make one SHALL be taken by a statement that records the attempt, so
the cap holds across instances and across redelivery.

The title column SHALL carry the same at-rest protection as the message body it is derived from
(`ouroboros/storage/message_field_crypto.py`). A provisional title is a slice of a message the
deployment chose to encrypt, and a plaintext column holding it is that message escaping the choice.
Listing SHALL NOT sort, filter or search on the title in SQL, which an encrypted column cannot
answer.

#### Scenario: A listing renders before any model has run

- **WHEN** a conversation's first message has been recorded and no titling work has completed
- **THEN** the listing shows that conversation with a title derived from that message
- **AND** the title's origin is the provisional one

#### Scenario: A generated title replaces the provisional one

- **WHEN** a generated title arrives for a conversation whose title is still provisional
- **THEN** the stored title becomes the generated one
- **AND** its origin becomes the generated one

#### Scenario: A late generated title does not overwrite a rename

- **WHEN** an owner renames a conversation and a generated title for it arrives afterwards
- **THEN** the stored title is still the owner's
- **AND** the statement that would have written the generated one matched no row

#### Scenario: A rename replaces a generated title

- **WHEN** an owner renames a conversation that already carries a generated title
- **THEN** the stored title is the owner's and its origin says so

#### Scenario: The title is protected as the message is

- **WHEN** the deployment encrypts message fields and a provisional title is stored
- **THEN** the stored title is protected the same way as the message body it came from

### Requirement: Retirement is a flag flip and never a delete

Retiring a conversation SHALL clear the record's active flag and SHALL leave the row in place. The
chat axis keys rows that outlive the conversation: with `messages`, `context_summaries`,
`task_episodes` and `tool_invocations` all keyed by chat, deleting the record orphans every one of
them while leaving the id reachable, and the next message on that id would mint a second record with
a fresh provisional title for a conversation that already has a history.

Retirement SHALL NOT be clearing. It decides what the owner's listing contains and removes no
stored row, and a client that names a retired chat id SHALL still read its history unchanged.

The surface SHALL read the record first, so a retirement naming a conversation that does not exist
is a missing resource rather than a silent no-op, and SHALL answer a successful one with the record
as it now stands: the caller changed a row it is about to render, and making it ask again for what it
just wrote is a second round trip for nothing. Retiring an already retired conversation SHALL be
accepted rather than refused: the requested state is the state it is in. Restoring SHALL be the same
operation with the flag set the other way, so an irreversible one-click action is not invented for a
row that is already in hand.

#### Scenario: Retirement preserves the row and the history

- **WHEN** an owner retires one of their conversations
- **THEN** the record is marked inactive and its row still exists
- **AND** the messages and the committed summary recorded under that chat are unaffected

#### Scenario: A retired conversation leaves the listing

- **WHEN** an owner lists their conversations after retiring one
- **THEN** the retired conversation is not in the listing
- **AND** the retirement itself answered with the record as it now stands

#### Scenario: Retiring what does not exist is a missing resource

- **WHEN** a retirement names a chat id that has no record under the requesting owner and agent
- **THEN** the request is refused as a missing resource
- **AND** no row is written

### Requirement: Absence is stated by the repository and interpreted by the surface, and a refusal is not an absence

A lookup for a conversation that does not exist, or that exists under another owner or another of the
owner's agents, SHALL return an absence value rather than raising and rather than substituting
another conversation. The HTTP surface SHALL translate that absence into a not-found response that
discloses no other owner's record. The repository states what the table holds; the surface decides
what holding nothing means to a caller.

A refusal SHALL NOT be spelled as an absence. A rename carrying an empty title, and a request naming
a chat id the chat id policy (`ouroboros/contracts/chat_id_policy.py`) calls synthetic, SHALL each be
refused as a malformed request. Answering either with not-found would tell the caller the
conversation does not exist, when the truth is that it may not be asked for — the same property the
browser boundary enforces on the way in, stated once more where a caller could otherwise reach a
skill-bridge or A2A conversation by naming its id.

#### Scenario: A lookup for an unknown conversation returns absence

- **WHEN** a chat id that no row carries is looked up for an owner and agent
- **THEN** the repository returns the absence value

#### Scenario: A lookup across owners returns absence

- **WHEN** an owner looks up a chat id that exists under a different owner
- **THEN** the repository returns the absence value
- **AND** the other owner's conversation is not returned

#### Scenario: A synthetic chat id is refused, not reported absent

- **WHEN** a request names a chat id the chat id policy calls synthetic
- **THEN** it is refused as malformed rather than answered as not found
- **AND** no row is read or written

#### Scenario: An empty title is refused

- **WHEN** a rename carries an empty title
- **THEN** it is refused as malformed
- **AND** the stored title is unchanged

### Requirement: The registry package names no transport

The package SHALL import no user-facing transport, no `supervisor` and no `multiuser_config`,
statically or lazily. The dependency runs the other way: the browser transport calls the registry to
record a conversation and its provisional title.

The owner SHALL arrive in the request header whose name is a constant of the owner-scope contract
(`ouroboros/contracts/owner_scope.py`), read by the transport and by the one owner resolver every
owner-bearing surface installs (`infra/utils/http/fastapi.py`), so sharing the constant is not a
dependency on the transport. Whether a missing header is refused SHALL be that resolver's answer,
read per request: the configured owner while the deployment is not declared multi-user, a forbidden
response once it is. No composition root SHALL hand the surface a fallback owner.

The registry SHALL be given the provisional title as a value. It SHALL NOT read a message row or a
transport payload to derive one, which would make it a second reader of the message shape and a
second place the excerpt rule is spelled.

#### Scenario: The registry package names no transport

- **WHEN** the package's import graph is inspected
- **THEN** none of its imports reaches a user-facing transport, the supervisor, or `multiuser_config`

#### Scenario: A missing header is refused in the multi-user mode

- **WHEN** the deployment is declared multi-user and a request arrives without the owner header
- **THEN** the request is refused with a forbidden response
- **AND** no row is read or written

#### Scenario: The configured owner serves the single-owner deployment

- **WHEN** the deployment is not declared multi-user and a request arrives without the owner header
- **THEN** the request is served under the configured owner

#### Scenario: The provisional title is supplied, not derived

- **WHEN** the registry records a conversation
- **THEN** the title it stores is the value its caller passed
- **AND** it reads no message row to compute one

### Requirement: The surface is a router each composition root includes under its own prefix

The registry SHALL expose an `APIRouter`, not an application, offering listing, rename and retirement
for the requesting owner and one named agent. Each composition root SHALL include it under its own
prefix on the application it builds and SHALL install the trace middleware there, because a router
mounted as a sub-application inherits none of the enclosing application's request instrumentation.
The single-process runtime (`server.py`) and the config registry tier
(`multiuser_config/config_registry/app.py`, which already includes the agent registry and the owner
settings writer) SHALL each include it, so the operations are the same in both deployment modes, and
every path they mount SHALL be named in that tier's entry in the mode's tier enumeration and
routed to that tier by the deployment address.

The router's module SHALL hold no repository: its routes SHALL receive one per request over the
shared engine holder (`infra/async_engine.py`), because surfaces are mounted at import while the
engine is composed at start-up. It SHALL compose no engine of its own, so it inherits the store's
failover rotation and its connection settings unchanged, and no message it raises SHALL name a
connection URL or a credential within one.

The agent SHALL be named by the request path and never by a header — the owner is what a fronting
proxy authenticated, the agent is a resource the caller chose — and SHALL be verified against the
agent registry, so a request naming an agent the owner does not have is a missing resource. Isolation
SHALL NOT rest on that verification; it rests on every statement naming the owner and the agent.

Listing SHALL return the owner's active conversations for the named agent, newest record first, with
a deterministic tie-break, because chat ids are opaque strings after
`accept-opaque-browser-chat-ids` and a client cannot order them itself. The order is the registry's
own creation order and not the conversations' activity: the record carries no last-activity value,
for the reason the first requirement gives.

The group is the **third owner-bearing route group**. Invariant 3 admits exactly two beside the
default headless router and requires the same explicit product decision for a further one; this
capability is that decision, and it SHALL be recorded in `docs/INVARIANTS.md` rather than left to the
route table. It is for the browser surface only: Dialogs and A2A present no conversation list and
keep minting the chat ids they mint today, so no record is written for them.

#### Scenario: Both modes offer the same operations

- **WHEN** the surface served by the single-process runtime is compared with the one the tier serves
- **THEN** the two offer the same operations, built from the same definitions
- **AND** each application carries the trace middleware it installed itself

#### Scenario: The router holds no repository

- **WHEN** the router module is imported before any engine is composed
- **THEN** the import succeeds and no repository is constructed
- **AND** a request served later receives one built on the shared holder's engine

#### Scenario: The listing is ordered and bounded to active conversations

- **WHEN** an owner lists their conversations for one agent
- **THEN** the response contains that owner's active conversations for that agent only
- **AND** they are ordered newest record first, deterministically among records created together

#### Scenario: An agent the owner does not have is a missing resource

- **WHEN** a request names an agent belonging to a different owner
- **THEN** it is refused as a missing resource
- **AND** the statements that would have run name the requesting owner's id

#### Scenario: The third route group is recorded

- **WHEN** the invariant governing owner-bearing route groups is read
- **THEN** it names this router beside the agent registry and the owner settings writer
- **AND** the routes served match the ones it names

#### Scenario: A failure names no credential

- **WHEN** the surface fails because the engine it was asked for cannot be provided
- **THEN** the message contains no connection URL and no credential

