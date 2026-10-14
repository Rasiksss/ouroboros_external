# chat-history-visibility Specification

## Purpose

One predicate decides what conversation history contains: a stored message belongs to it unless it
is a progress message. The decision is materialised in `messages.chat_history_visible` and
recomputed on metadata updates; no read surface filters by chat id sign. A read is bounded by the
owner, the agent and the chat together, which is a key rather than a visibility filter. Visibility
in history does not imply live delivery.

## Requirements

### Requirement: One predicate decides what conversation history contains

The system SHALL decide whether a stored message belongs to conversation history by exactly one
predicate: the message is not a progress message. No other property of the message — its chat id,
its sign, its source, or the transport that delivered it — SHALL affect that decision. The predicate
SHALL be materialised in the indexed `messages.chat_history_visible` column at write time and
recomputed whenever a message's metadata is updated.

Which conversation a row belongs to is a different question, answered by the row's `chat_id` key
column and by the predicate of the read — never by this flag. The two SHALL stay apart because they
have different lifetimes: the flag is decided once, at write, and never re-decided, while
membership of a conversation is what every read narrows on afresh. Folding the chat into the flag
would freeze at insert time a decision each reader has to make for itself, and would make
`_chat_history_visible` (`ouroboros/storage/sql_runtime_store.py::_chat_history_visible`) depend on identity rather
than on content.

The chat axis SHALL be fixed when the row is inserted, from the writer's `OwnerScope`, and
`update_message_metadata` (`ouroboros/storage/sql_runtime_store.py::update_message_metadata`) SHALL recompute the flag
without touching it. A metadata edit that moved a row between conversations would take messages out
of a history a person has already read, and would do it through the one write path that exists for
correcting an unrelated field.

#### Scenario: A synthetic chat id no longer hides a message

- **WHEN** a non-progress message is appended with a negative chat id
- **THEN** it is stored as visible in chat history
- **AND** a chat-history read under that message's own conversation returns it

#### Scenario: Progress messages stay out regardless of chat id

- **WHEN** a progress message is appended, with a positive or a negative chat id
- **THEN** it is stored as not visible in chat history
- **AND** the chat-history read surface omits it

#### Scenario: Updating a message recomputes its visibility

- **WHEN** a stored message's metadata is updated so that it is no longer a progress message
- **THEN** its stored visibility flag reflects the new metadata

#### Scenario: A metadata update cannot move a row between conversations

- **WHEN** a stored message's metadata is updated with a different `chat_id` inside it
- **THEN** the row's `chat_id` column is unchanged
- **AND** the message is still returned by a read of the conversation it was written into

### Requirement: No read surface applies a second visibility filter

The system SHALL NOT expose any parameter or code path that removes messages from an owner-facing
read on the basis of the chat id's sign. The message-listing and message-counting operations of the
runtime store interface SHALL NOT accept an A2A-exclusion argument, and the owner message list, the
owner message count, and the chat-history watermark SHALL each reflect the single visibility
predicate.

Progress messages are the one thing outside that predicate, because they are not conversation
history: they are the owner's live view of their own agent working. When an owner-facing read asks
for them explicitly, it SHALL receive only the ones belonging to the owner's own conversations —
never those an agent emitted while serving a remote peer.

Once history is keyed by chat, that carve-out
(`ouroboros/storage/sql_runtime_store.py`) is no longer what keeps a remote peer's
progress out of an owner's read; the chat predicate is. It SHALL survive as the second enforcement
point rather than be deleted as redundant, on the same grounds the dedup identity keeps both its
fold and its predicate: the derivation makes the wrong row impossible, and the filter makes the
wrong *result* impossible if a later change to the predicate regresses.

#### Scenario: Owner message list includes synthetic-id traffic

- **WHEN** the store holds owner messages with positive and with negative chat ids
- **THEN** the owner message list of each conversation returns that conversation's own

#### Scenario: A remote peer's progress is not the owner's

- **WHEN** an owner-facing read asks for progress messages
- **AND** the store holds progress rows for the owner's chat and for a synthetic A2A conversation
- **THEN** only the owner's own progress rows are returned

#### Scenario: The watermark counts every non-excluded message

- **WHEN** a message is appended to a conversation whose chat id is negative
- **THEN** the chat-history watermark signature read under that conversation's scope reports one
  more message

#### Scenario: The store interface exposes no exclusion switch

- **WHEN** the runtime store's listing and counting operations are inspected
- **THEN** neither accepts a parameter that filters by chat id sign

### Requirement: The predicate governs the rows written under it, and no others

The stored visibility flag SHALL be decided when a message is written and recomputed only when that
message's metadata changes. No migration SHALL walk existing rows to re-decide them under a later
predicate. Rows written before this predicate therefore keep the flag they were given — synthetic
chat ids among them, which an earlier predicate hid — and they are absent from conversation history
permanently rather than temporarily. The revision that adds the conversation axis SHALL NOT take
the occasion to re-decide the flag on the rows it is already rewriting.

That revision recovers every row's conversation or refuses to run. A message's chat id lives in
`metadata_json`, which is encrypted at rest wherever chat encryption is on
(`ouroboros/storage/message_field_crypto.py`, postgres only), and the revision decrypts it rather
than deciding from the envelope: an unreadable blob aborts before any DDL, because parking a message
is heavier than parking an episode — it is a person's visible history leaving every conversation at
once. Only a row that parses and names no chat is parked, on the empty chat `0008` already parks
with, which no `OwnerScope` can carry
(`infra/migrations/versions/0008_owner_scope_rekey.py`). Such a row SHALL NOT be
deleted, and the count SHALL be stated for an operator rather than left for a deployment to
discover.

The rows are hidden, not exempt: every operation that acts on stored messages regardless of
visibility SHALL continue to act on them.

#### Scenario: A message written under an earlier predicate keeps its flag

- **WHEN** a non-progress message with a negative chat id was stored as not visible by an earlier
  predicate
- **THEN** it stays not visible
- **AND** conversation history does not return it

#### Scenario: Both history surfaces agree about it

- **WHEN** such a message is in the store
- **THEN** neither the chat-history read nor the owner's own message list returns it

#### Scenario: An unrecoverable conversation is parked, not guessed

- **WHEN** the revision meets a row whose chat id it cannot read from a plain column
- **THEN** the row still exists after migration
- **AND** its chat axis matches no scope any transport can mint

### Requirement: Visibility in history does not imply live delivery

The system SHALL keep the routing suppression that prevents synthetic negative chat ids from
reaching browser broadcasts, outbound chat events, typing indicators, photo and file sends, and
dialogue delivery. History SHALL be served from the message store rather than from the live event
stream, so a message MAY be present in history without ever having been delivered to a human
channel.

#### Scenario: A synthetic-id message is recorded but not broadcast

- **WHEN** an agent reply is produced for a negative chat id
- **THEN** no browser broadcast, outbound chat event, or dialogue send is issued for it
- **AND** the message is readable from chat history afterwards

### Requirement: History reads are bounded by ownership, which is not a visibility filter

Every read of conversation history SHALL be bounded by the ownership axes of the reader's scope, so
that a row belonging to another owner, another agent, or another conversation is never considered.
For `messages` those axes are `owner_id`, `agent_id` and `chat_id`: the chat joins the bound
because two conversations of one owner and agent are two histories, and because the axis that
distinguishes one deployment's peers from another's is today the only one that reaches no predicate.

That bound SHALL NOT be expressed as, or confused with, the visibility predicate: within one owner,
one agent and one conversation, whether a message belongs to conversation history SHALL still be
decided by the single visibility predicate and by nothing else.

The bound SHALL be one shared named constant, so that no statement can narrow on two axes and
forget the third. It SHALL be its own constant rather than a reuse of `_SCOPED`
(`ouroboros/storage/sql_runtime_store.py`), which also names `surface_id` — a column `messages`
does not have and SHALL NOT gain for this, since a conversation is already unique within its owner
and agent. The axis a read narrows on SHALL be the row's `chat_id` column, filled from the writing
scope; the `chat_id` inside `metadata_json` stays what the chat id policy calls it, a routing fact
(`ouroboros/contracts/chat_id_policy.py`), and SHALL reach no predicate.

A read that deliberately crosses conversations SHALL be a named exception with its reason recorded,
and SHALL feed neither a prompt nor an owner-facing history — the crash-replay journal sweep
(`transport/journal.py`), which must see every conversation's unprocessed rows, and the
deployment-wide owner/agent enumeration (`ouroboros/storage/sql_runtime_store.py`) are such
exceptions. An implicit widening SHALL NOT be available: a read SHALL NOT reach across
conversations by leaving an axis empty or defaulted.

#### Scenario: Another owner's messages are never considered

- **WHEN** messages exist under two different `owner_id` values and history is read under one of them
- **THEN** only that owner's messages are returned

#### Scenario: Another conversation of the same owner is never considered

- **WHEN** one owner and agent hold messages in two conversations and history is read under one of
  them
- **THEN** only that conversation's messages are returned
- **AND** the same holds for the committed summary the read assembles beside them

#### Scenario: The bound does not hide a visible message

- **WHEN** history is read under the owner, agent and conversation that the messages were written
  under
- **THEN** every message the visibility predicate admits is returned, whatever its chat id, its sign,
  or its source

#### Scenario: One constant carries the message predicate

- **WHEN** the SQL of the message reads, writes and deletes is inspected
- **THEN** the owner-agent-chat predicate appears as a single shared constant rather than as repeated
  inline text
- **AND** no statement serving conversation history filters on the ownership pair alone

#### Scenario: A cross-conversation read is named, not incidental

- **WHEN** a read of `messages` crosses conversations
- **THEN** it is one of the named exceptions, with its reason recorded at the call site
- **AND** it feeds no prompt and no owner-facing history
