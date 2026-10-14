# a2a-ingress-history Specification

## Purpose

Inbound A2A (agent-to-agent) traffic is recorded into conversation history at ingress: accepted
messages persist as inbound conversation messages carrying the A2A source, a chat id derived
deterministically from the task id, and the task and context ids in transport metadata. Recording is
deduplicated by message id, never breaks the A2A transaction on failure, and attributes every
message to its remote caller.

## Requirements

### Requirement: An accepted A2A message is recorded at ingress

The A2A ingress SHALL persist the incoming message as a conversation message before the agent's
reply is awaited. The record SHALL be an inbound message carrying the A2A source, the derived chat
id, the task id, and the A2A context id in its transport metadata. A message the seam *confirms* it
did not accept — the inbox raises — SHALL NOT remain recorded. A submission whose outcome is
unknown — the submitting call is cancelled while the message may still reach the inbox — SHALL
leave the record in place, because the accepted work would otherwise be missing from the
conversation it belongs to; the delivery id below is what reconciles a later retry with it.

#### Scenario: A dispatched message reaches history

- **WHEN** an A2A request is submitted to the supervisor successfully
- **THEN** an inbound message with the request's text and the A2A source is present in chat history
- **AND** it carries the task id and the A2A context id

#### Scenario: An empty request records nothing

- **WHEN** an A2A request arrives with no text and is rejected
- **THEN** no message is written

#### Scenario: A request rejected for concurrency records nothing

- **WHEN** an A2A request is rejected because too many tasks are already running
- **THEN** no message is written

#### Scenario: A refused submission records nothing

- **WHEN** submitting the envelope to the inbox raises, which the seam defines as "not accepted"
- **THEN** no message is written

#### Scenario: A cancelled submission keeps its record

- **WHEN** the submitting call is cancelled while the message may still reach the inbox
- **THEN** the inbound message remains in chat history
- **AND** a redelivery of it neither adds a second row nor starts a second task

#### Scenario: A request that times out is still recorded

- **WHEN** a request is accepted and the agent does not reply within the hard timeout
- **THEN** the inbound message remains in chat history

### Requirement: The A2A chat id is derived from the task id

The A2A ingress SHALL derive the chat id for a request by hashing its task id into a negative range
that does not overlap the range allocated to skill-bridge internal conversations, and SHALL fall
back to hashing a freshly generated unique value when the task id is empty. The ingress SHALL NOT
use a process-global counter. The derivation SHALL be stable across process restarts.

What the derivation *is* changes with this change, while the rule above does not. Until now the
value was a routing label: it selected a response subscription and suppressed live delivery, and it
reached no SQL predicate, because `messages` carried it only inside `metadata_json`. Once `messages`
narrows on the owner, the agent and the chat together, the value `derive_a2a_chat_id`
(`transport/a2a/executor.py::derive_a2a_chat_id`) returns is the predicate that decides which rows an A2A run reads
and which rows its reply joins.

Two properties that were conveniences therefore become load-bearing, and SHALL be treated as such.
Stability across processes is one: a retry or a crash replay landing on another instance must reach
the same rows, which is why `transport/a2a/executor.py::_A2AEnvelopeBuilder` prefers the
stored `chat_id` and re-derives only as a fallback. Disjointness from the skill bridge's window
(`ouroboros/contracts/chat_id_policy.py`) is the other: the two allocators sharing the
negative range would now merge two agents' prompt context rather than two rows in a history nobody
read per chat.

The collision the modulus admits SHALL be priced at its new cost where the modulus is defined. The
note at `transport/a2a/executor.py` says today that a collision "Across all history it merges
two conversations"; under this change that merge is paid in the model's context window, because the
merged rows are what `ouroboros/agent/context.py` reads for `## Recent chat`. Neither
`_A2A_CHAT_ID_MODULUS` nor the collision probability is changed here — only the consequence, which
SHALL be recorded rather than left to be rediscovered.

Whether the A2A **context id** rather than the task id should be the chat axis is explicitly out of
scope for this change. `contextId` is what the A2A protocol uses to group a multi-turn remote
conversation, so keying on it would give a peer one conversation across its tasks instead of one per
task — a change to what a remote caller sees of its own history, not to the cross-person exposure
this change exists to close, and one that interacts with the chat registry's titles and with the
browser chat id policy being decided in their own changes. It stays open at no migration cost: every
A2A row already records the context id (`transport/a2a/executor.py`, `transport={"a2a_context_id":
context_id}`), so the axis can be re-keyed later from data that is already stored.

#### Scenario: The same task id yields the same chat id

- **WHEN** the chat id is derived twice for the same task id, in different processes
- **THEN** both derivations produce the same value

#### Scenario: Different task ids yield different conversations

- **WHEN** two requests with different task ids are dispatched
- **THEN** their messages carry different chat ids
- **AND** they belong to different conversations

#### Scenario: The derived id avoids the skill-bridge range

- **WHEN** a chat id is derived for any task id
- **THEN** it is negative
- **AND** it lies outside the range from which skill-bridge internal chat ids are allocated

#### Scenario: A missing task id still yields a usable id

- **WHEN** a request arrives with an empty task id
- **THEN** a negative chat id in the same range is produced
- **AND** it does not collide with the id derived for any non-empty task id in the same process

#### Scenario: The derived id is the predicate an A2A run reads under

- **WHEN** an A2A run assembles its prompt context
- **THEN** the rows it reads are those whose stored chat equals the id derived from its own task id
- **AND** a row of another task, another surface, or another owner is never considered

#### Scenario: A replayed request reaches the rows it already wrote

- **WHEN** the crash-replay sweep rebuilds an envelope for a stored A2A request
- **THEN** the rebuilt scope carries the chat the row was written under
- **AND** the resubmitted run reads the turns already recorded for that task rather than an empty
  conversation

#### Scenario: The collision's new cost is recorded beside the modulus

- **WHEN** the derivation's modulus and the note beside it are inspected
- **THEN** the note states that a collision merges two conversations' prompt context, not only their
  stored history

### Requirement: A redelivered message does not duplicate, a second one does not vanish

The A2A ingress SHALL identify each delivery by the incoming *message* id, so that redelivering the
same message adds no second row while a further message in the same task is recorded as its own. A
caller that sends no message id SHALL get one derived from the task id and the message text, so
redelivery still deduplicates. That one id SHALL key both the history record and the submitted
envelope's client message id, so that the supervisor derives the same idempotent task id from it:
deduplicating history alone would leave a redelivery adding no row while queueing a second task for
work already running.

#### Scenario: Re-delivering the same message

- **WHEN** the same A2A message is dispatched twice
- **THEN** chat history holds exactly one inbound message for it
- **AND** the supervisor holds exactly one task for it

#### Scenario: A second message in the same task

- **WHEN** a second message is sent against a task that already has one
- **THEN** chat history holds both, in order
- **AND** each is submitted as its own task

### Requirement: The request and the reply share one conversation, one per task

An A2A request and the agent's final reply to it SHALL belong to the same conversation, so the
exchange reads as a dialogue rather than as two unrelated entries. That conversation SHALL be
identified by the chat id derived from the remote task id — the same value the scope carries — rather
than by the owner's session or by a separate stored conversation key, so that one remote exchange
stays distinguishable from another and from the session that outlives both.

This requirement survives unchanged in what it demands and changes entirely in what it buys. It was
a grouping in a history that no read filtered by; it is now the read predicate itself. The pairing of
a request with its reply is what makes an A2A run's `## Recent chat` its own — and, on the same
predicate, what keeps that exchange out of the owner's.

The value SHALL therefore be the same on both rows and SHALL come from one place on each. The
request's row is written at `transport/a2a/executor.py` under the envelope's own scope, whose
chat axis is `str(derive_a2a_chat_id(task_id))`; the reply is persisted by the supervisor
under the scope the run carries. Neither SHALL re-derive the value from a field of its own, because
two derivations of one identity are what lets a request and its reply land in two conversations
while every statement succeeds.

**BREAKING**: one task's conversation is now all an A2A run can see. A remote peer sending a second
task gets a run whose recent chat holds that task's turns only, and the owner's own conversations are
invisible to it in both directions. That regression is accepted as the price of narrowing the read
rather than worked around; a peer needing continuity across its tasks is the `contextId` question the
requirement above parks.

#### Scenario: Request and reply group together

- **WHEN** an A2A request completes and the agent's reply is recorded
- **THEN** both messages carry the same derived chat id

#### Scenario: Different task ids yield different conversations

- **WHEN** two A2A requests arrive under different task ids
- **THEN** their recorded messages carry different conversation ids

#### Scenario: A run reads its own task's turns and nothing else

- **WHEN** two A2A tasks are in flight for one owner and agent, and a run assembles its prompt
- **THEN** its `## Recent chat` holds the turns of its own task only
- **AND** the other task's turns, and the owner's browser and dialogs conversations, are absent

#### Scenario: The owner's history does not gain the exchange

- **WHEN** an A2A exchange completes and the owner reads their own conversation
- **THEN** neither the request nor the reply is returned
- **AND** both rows are still stored, reachable under the task's own chat

### Requirement: A2A messages are attributed to their remote caller

Every message recorded by the A2A ingress SHALL carry a sender label identifying the remote caller,
so that history surfaces render it distinguishably from a message the owner wrote. The label SHALL
NOT be empty.

The rule stands; the surface that reads it does not. The label existed so a history surface showing
an owner's messages *and* a remote peer's could tell them apart. No surface shows both any more: an
A2A row is reachable only under the chat `derive_a2a_chat_id` produced for its task, which neither
the browser identity resolver (`transport/browser/identity.py`, which refuses every value the chat
id policy calls synthetic) nor the Dialogs peer mint (`transport/dialogs/transport.py`) can name.

The label is nevertheless still required, on a reason that survives the narrowing: the one reader of
an A2A conversation is now the A2A run itself, and its prompt renders `[User]` for an unlabeled row
— which would have the agent read a remote peer's request as its owner's. The comment at
`transport/a2a/executor.py` already states that reason, and it is the whole of it after this
change.

The two scenarios below are rewritten rather than the requirement removed, because the requirement
is still enforced and still enforceable. The first was reachable in an owner's cross-conversation
context and is now reachable only in the A2A run's own; it is tightened to say so. The second — the
browser history record carrying the label — is unreachable at any scope a browser client can present,
and is replaced by the assertion that now holds in its place.

#### Scenario: The A2A run's own context shows the caller, not the owner

- **WHEN** an A2A message appears in the recent-chat context of the run serving its task
- **THEN** the rendered line names the remote caller rather than defaulting to the owner's label

#### Scenario: A browser history read carries no A2A record at all

- **WHEN** the browser chat history is read under a chat id a browser client presented
- **THEN** no A2A record is returned, whatever label it carries
- **AND** the label is still stored on that row, for the A2A run that does read it

#### Scenario: The label is written even though no human surface renders it

- **WHEN** an A2A request is recorded at ingress
- **THEN** its stored sender label is non-empty and names the remote caller
- **AND** the reason recorded beside it is the agent's own prompt, not a history surface

### Requirement: Recording an A2A message never breaks the A2A transaction

A failure to persist the incoming message — including an unavailable message store or a
database error — SHALL be logged with its traceback and SHALL NOT alter the A2A response, its task
lifecycle, or any event emitted to the caller.

#### Scenario: The store is unavailable

- **WHEN** persisting the incoming message raises
- **THEN** the task continues and the caller receives the agent's reply as if nothing had failed
- **AND** a warning with the traceback is logged

#### Scenario: No message store is configured

- **WHEN** the ingress runs without a drive root, as in unit tests
- **THEN** persistence is skipped without raising
- **AND** the dispatch proceeds
