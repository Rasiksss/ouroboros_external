# attachment-ownership Specification

## Purpose

Which owner an attachment record belongs to and who may reach it: the axes stored on the record,
the owner argument every attachment operation takes, the browser file routes, the tool paths, and
the single cross-owner exception granted to the expiry sweep.

## Requirements

### Requirement: An attachment record names the owner it belongs to

Every stored attachment record SHALL carry the ownership axes of the scope that created it —
`owner_id`, `agent_id`, `surface_id` and `chat_id` — taken from an `OwnerScope` the creating path
already holds and from nothing else. The axes SHALL be part of the record's normalized shape, not an
extra key on the stored payload: the record is rebuilt through normalization and written back by
compare-and-swap on every state change (staging → ready, draft → bound, bound → released, → deleting),
so a field the normalizer does not know is erased by the first of those transitions.

Every production writer SHALL supply the scope of the message the attachment belongs to: the browser
upload route the scope it resolved for that request, the Dialogs inbound path the scope it minted for
that message, the outbound attachment path the scope of the message being delivered, and the legacy
inline-image path the inbound envelope's scope.

#### Scenario: A stored record carries its creator's ownership

- **WHEN** an attachment is stored on any ingress or egress path
- **THEN** the persisted record carries the `owner_id` and `agent_id` of the scope that stored it
- **AND** it carries that scope's `surface_id` and `chat_id`

#### Scenario: Binding to a message does not erase the ownership

- **WHEN** a draft attachment is bound to a message, released back to draft, and moved to deleting
- **THEN** the record still carries the same ownership axes after each transition

### Requirement: Every attachment operation takes the owner scope as an argument

Each operation that reads, creates or mutates an attachment record SHALL take the owner scope as an
explicit argument rather than reading it from a holder, a module global, or the path the object sits
under. The argument SHALL be named `owner_scope`, because in this module `scope` already names the
storage scope passed to the provider.

An operation SHALL compare the record's `owner_id` and `agent_id` against the argument, and SHALL
treat a record whose pair differs as absent — the same answer a missing record produces — rather than
as forbidden. `surface_id` and `chat_id` are recorded as provenance and SHALL NOT be part of the
predicate, because one owner may legitimately reference one attachment from two conversations, and
the rule that decides which of them a given run may reach is a separate predicate one layer above
this one.

That layer is `attachment-reference-resolution`, which requires the record to name the chat the run
is in **and** deliberately admits an id bound to the current run whatever chat the record names. It
reaches the record through this operation first: `ouroboros/tools/attachments.py::resolve_reachable_attachment`
calls `get_attachment` and only then applies the chat criterion. A
chat-aware predicate here would answer absent, so the admission would become
unreachable — an exception this capability does not own would be deleted from underneath, with no
statement failing to say so.

The chat axis is also not a conversation on every record. A scope minted by
`new_core/identity/service_scope.py::ServiceScope.as_scope` carries the job name in that axis, and an
A2A chat id is derived per task by `transport/a2a/executor.py::derive_a2a_chat_id`. Comparing the
axis would strand every record created under such a scope permanently, and there is no repair for it:
attachment records are object-store JSON rebuilt by normalization on each transition, and this
capability already refuses to backfill axes onto records written before they existed.

This predicate is not a conversation boundary and SHALL NOT be relied on as one. On the Dialogs
surface `transport/dialogs/transport.py::_scope_for_chat` mints one `configured_owner_id()` and one
`configured_agent_id()` for every peer, so the pair separates deployments rather than people, and the
chat criterion above it is what keeps one peer out of another's uploads.

The reason SHALL be recorded where the predicate is defined —
`ouroboros/storage/chat_attachments.py::_owned_by` and the `AttachmentMetadata` axis comment above it
— and SHALL be the layering above, not the width of a history read. The reason recorded there until
now was that an owner's history "deliberately shows an owner's whole conversation across surfaces and
chats"; once `messages` narrows on the owner, the agent and the chat together, that sentence is false
of the chat axis, and a rule justified by it would be a rule with no reason left.

A record that carries no `owner_id` SHALL be treated as absent as well. It is not deleted by the
comparison, and it remains reapable by expiry.

#### Scenario: Reading another owner's id reports absence

- **WHEN** an attachment is read under a scope whose ownership pair differs from the record's
- **THEN** the operation reports the attachment as absent
- **AND** the answer is indistinguishable from the answer for an id that was never stored

#### Scenario: Binding another owner's draft is refused and changes nothing

- **WHEN** a message under one owner tries to bind a draft attachment stored under another
- **THEN** the bind is refused
- **AND** the draft record is unchanged and still bound to nothing

#### Scenario: Deleting another owner's draft is refused and the bytes survive

- **WHEN** a delete is requested under one owner for a draft stored under another
- **THEN** the delete reports the attachment as absent
- **AND** the record and its stored object are still present

#### Scenario: An unattributed record is not served

- **WHEN** a record carrying no `owner_id` is read under any scope
- **THEN** it is reported as absent
- **AND** it is still present in storage

#### Scenario: Narrowed history does not narrow the attachment predicate

- **WHEN** an attachment operation is called under a scope whose owner pair matches the record and
  whose `chat_id` and `surface_id` do not, after message history has been narrowed to one chat
- **THEN** the operation returns the record rather than reporting absence
- **AND** whether the caller may use it is decided above this layer, by the chat criterion that
  admits an id bound to the running task whatever chat its record names

#### Scenario: A record minted under a service scope stays reachable

- **WHEN** an attachment is stored under a scope whose `chat_id` is a job name rather than a
  conversation, and is later read under the owner pair that stored it
- **THEN** the record is returned
- **AND** no read of it depends on the value of that axis

#### Scenario: The pair does not separate two Dialogs peers, and the layer above does

- **WHEN** a run started by one Dialogs peer asks for a record another peer of the same deployment
  uploaded, both carrying the deployment's configured owner and agent
- **THEN** this predicate reports the record as present
- **AND** the tool refuses under the chat criterion above it, and the object is not read

#### Scenario: The recorded reason is the one that is true

- **WHEN** the justification written beside the predicate is read
- **THEN** it gives the layering above — one attachment legitimately referenced from two
  conversations, and the run-bound admission that lives one layer up — as why surface and chat stay
  out
- **AND** it makes no claim that an owner's message history spans several chats

### Requirement: The browser file routes resolve identity before touching an upload

The attachment download, upload and delete routes SHALL resolve the caller's scope through the
installed browser identity resolver, per request, before they build a storage provider or touch any
object. An identity the instance cannot use SHALL be refused with a `403` naming the reason. A record
that names another owner SHALL produce the route's not-found answer.

The upload route SHALL record the scope it resolved on the attachment it stores, so that the file it
just accepted is retrievable by the caller that uploaded it and by nobody else.

The WebSocket path that binds attachment ids into a message SHALL pass the scope minted at that
connection's handshake, so a connection cannot bind another owner's draft into its own message.

#### Scenario: Downloading another owner's attachment id is not found

- **WHEN** a client presenting one owner's headers requests an attachment id stored under another
  owner
- **THEN** the response is `404`
- **AND** no bytes and no filename of that attachment are disclosed

#### Scenario: Deleting another owner's upload is not found and destroys nothing

- **WHEN** a client presenting one owner's headers deletes an upload id stored under another owner
- **THEN** the response is `404`
- **AND** the other owner's record and object are still present

#### Scenario: An upload is retrievable by its uploader

- **WHEN** a client uploads a file and then downloads the id it was given
- **THEN** the download succeeds under the same headers
- **AND** the same download under a different owner's headers is `404`

#### Scenario: A refused identity is a 403, not a 404

- **WHEN** a file route is called while the mode is on and the owner header is absent
- **THEN** the response is `403` and states the reason
- **AND** no storage provider is built

### Requirement: An object with no record is not served

The download route SHALL serve only objects that have a metadata record whose ownership matches the
caller. An object with no record names no owner and SHALL NOT be served, whatever branch of the path
resolution reached it — a `generated/` key, a public alias, or a bare key that merely exists.

#### Scenario: A bare object without a record is not downloadable

- **WHEN** an object exists under the uploads scope with no metadata record and a client requests its
  path
- **THEN** the response is `404`
- **AND** the object is not read

### Requirement: Tool paths compare the attachment's owner too

Every tool path that materializes a stored attachment — outbound file sending, attachment reading and
image analysis — SHALL pass the running task's scope and SHALL refuse an attachment whose record
names another owner. This predicate is additional to the rule that the attachment must belong to the
chat the run is in (or be bound to the run itself); neither replaces the other.

The owner predicate stays the pair `owner_id` + `agent_id`. It is not widened and not narrowed by the
chat rule: the chat rule decides which of the owner's own attachments a run may reach, and this one
decides whose attachments they are.

#### Scenario: A task cannot send another owner's attachment

- **WHEN** a task asks to send a stored attachment id whose record names another owner
- **THEN** the tool refuses
- **AND** nothing is delivered and nothing is published

#### Scenario: The task-membership rule still holds

- **WHEN** a task asks for an attachment id that is not among the attachments bound to it, whatever
  its owner
- **THEN** the tool refuses

#### Scenario: The chat-membership rule still holds

- **WHEN** a task asks for an attachment id that is neither bound to it nor a record of the chat it
  is running in, whatever its owner
- **THEN** the tool refuses

### Requirement: The expiry sweep is the one named cross-owner attachment path

The bounded expiry sweep SHALL delete expired attachment records and their objects regardless of
which owner they name, and SHALL be the only attachment path that does so. It SHALL return no
attachment content to any caller, and its reason for crossing owners SHALL be recorded where it is
defined: an expired record belongs to nobody, and a per-owner sweep would either leave unattributed
records forever or repeat a bounded page scan once per owner.

#### Scenario: Expired records of several owners are reaped by one sweep

- **WHEN** expired records exist under two different owners
- **THEN** one sweep pass deletes them within its page budget
- **AND** no owner-bearing content is returned to a caller

### Requirement: A record written before the axes existed is refused, not backfilled

Records written before this change name no owner and SHALL NOT be given one: there is no backfill,
and such a record SHALL NOT be deleted on sight but SHALL be removed by the expiry sweep at its TTL.

Whether it is *served* depends on how many owners the instance has. Where it serves several, a
record with empty axes SHALL answer every read exactly as an absent one — the owner is unknown and
any answer would name the wrong person. Where it serves one, that record SHALL be served to that
owner: there is a single candidate, so refusing withholds an owner's own files from them and buys
no isolation. This is the one predicate that reads the mode, and it reads the same switch the key
layout does.

#### Scenario: A single-owner instance serves what the previous release wrote

- **WHEN** an instance serving one owner reads a record carrying no `owner_id`
- **THEN** the record is returned to that owner
- **AND** the object its `storage_key` names is served

#### Scenario: An unowned record is refused and reaped where owners are several

- **WHEN** an instance serving several owners is asked for a record carrying no `owner_id`
- **THEN** the answer is the one an absent record gets
- **AND** the record is removed by the expiry sweep at its TTL
