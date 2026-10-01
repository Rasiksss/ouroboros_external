# chat-history-visibility Specification

## Purpose

One predicate decides what conversation history contains: a stored message belongs to it unless it
is a progress message. The decision is materialised in `messages.chat_history_visible` and
recomputed on metadata updates; no read surface filters by chat id sign, and clearing owner history
removes synthetic-id messages too. Visibility in history does not imply live delivery.

## Requirements

### Requirement: One predicate decides what conversation history contains

The system SHALL decide whether a stored message belongs to conversation history by exactly one
predicate: the message is not a progress message. No other property of the message — its chat id,
its sign, its source, or the transport that delivered it — SHALL affect that decision. The predicate
SHALL be materialised in the indexed `messages.chat_history_visible` column at write time and
recomputed whenever a message's metadata is updated.

#### Scenario: A synthetic chat id no longer hides a message

- **WHEN** a non-progress message is appended with a negative chat id
- **THEN** it is stored as visible in chat history
- **AND** it is returned by the chat-history read surface

#### Scenario: Progress messages stay out regardless of chat id

- **WHEN** a progress message is appended, with a positive or a negative chat id
- **THEN** it is stored as not visible in chat history
- **AND** the chat-history read surface omits it

#### Scenario: Updating a message recomputes its visibility

- **WHEN** a stored message's metadata is updated so that it is no longer a progress message
- **THEN** its stored visibility flag reflects the new metadata

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

#### Scenario: Owner message list includes synthetic-id traffic

- **WHEN** the store holds owner messages with positive and with negative chat ids
- **THEN** the owner message list returns both

#### Scenario: A remote peer's progress is not the owner's

- **WHEN** an owner-facing read asks for progress messages
- **AND** the store holds progress rows for the owner's chat and for a synthetic A2A conversation
- **THEN** only the owner's own progress rows are returned

#### Scenario: The watermark counts every non-excluded message

- **WHEN** a message with a negative chat id is appended
- **THEN** the chat-history watermark signature's message count increases

#### Scenario: The store interface exposes no exclusion switch

- **WHEN** the runtime store's listing and counting operations are inspected
- **THEN** neither accepts a parameter that filters by chat id sign

### Requirement: Clearing owner history removes every visible message

The system SHALL remove messages with synthetic negative chat ids when owner chat history is
cleared, subject to the same preservation and watermark rules that apply to every other message. No
message SHALL be exempt from deletion on account of its chat id.

#### Scenario: Clearing removes synthetic-id messages

- **WHEN** the store holds messages with positive and negative chat ids and owner chat history is
  cleared
- **THEN** no message of either kind remains

#### Scenario: Preservation still applies

- **WHEN** owner chat history is cleared with a message id marked for preservation and that message
  carries a negative chat id
- **THEN** that message is retained
- **AND** the other messages are removed

### Requirement: The predicate governs the rows written under it, and no others

The stored visibility flag SHALL be decided when a message is written and recomputed only when that
message's metadata changes. No migration SHALL walk existing rows to re-decide them under a later
predicate. Rows written before this predicate therefore keep the flag they were given — synthetic
chat ids among them, which an earlier predicate hid — and they are absent from conversation history
permanently rather than temporarily.

The rows are hidden, not exempt: every operation that acts on stored messages regardless of
visibility, clearing owner history included, SHALL continue to act on them.

#### Scenario: A message written under an earlier predicate keeps its flag

- **WHEN** a non-progress message with a negative chat id was stored as not visible by an earlier
  predicate
- **THEN** it stays not visible
- **AND** conversation history does not return it

#### Scenario: Both history surfaces agree about it

- **WHEN** such a message is in the store
- **THEN** neither the chat-history read nor the owner's own message list returns it

#### Scenario: Clearing still removes it

- **WHEN** owner chat history is cleared
- **THEN** that message is deleted like any other

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
