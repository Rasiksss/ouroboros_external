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
id, the task id, and the A2A context id in its transport metadata. A message whose submission is
*confirmed* refused SHALL NOT remain recorded. A submission whose outcome is unknown — the
supervisor cannot read back whether the task committed, or the submitting call is cancelled while
the commit may still land — SHALL leave the record in place, because the accepted work would
otherwise be missing from the conversation it belongs to; the delivery id below is what reconciles
a later retry with it.

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

- **WHEN** submitting the envelope to the supervisor raises a confirmed failure
- **THEN** no message is written

#### Scenario: A submission of unknown outcome keeps its record

- **WHEN** the supervisor cannot confirm whether the submission committed
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
derived from the task's chat id rather than from the owner's session, so that one remote exchange
can be exported or deleted without taking the session with it.

#### Scenario: Request and reply group together

- **WHEN** an A2A request completes and the agent's reply is recorded
- **THEN** both messages carry the same conversation id

#### Scenario: Different task ids yield different conversations

- **WHEN** two A2A requests arrive under different task ids
- **THEN** their recorded messages carry different conversation ids

### Requirement: A2A messages are attributed to their remote caller

Every message recorded by the A2A ingress SHALL carry a sender label identifying the remote caller,
so that history surfaces render it distinguishably from a message the owner wrote. The label SHALL
NOT be empty.

#### Scenario: The agent's context shows the caller, not the owner

- **WHEN** an A2A message appears in the agent's recent-chat context
- **THEN** the rendered line names the remote caller rather than defaulting to the owner's label

#### Scenario: The browser history record carries the label

- **WHEN** the browser chat history is read
- **THEN** the A2A record carries the A2A source and the caller's sender label

### Requirement: Recording an A2A message never breaks the A2A transaction

A failure to persist the incoming message — including an unavailable supervisor runtime or a
database error — SHALL be logged with its traceback and SHALL NOT alter the A2A response, its task
lifecycle, or any event emitted to the caller.

#### Scenario: The store is unavailable

- **WHEN** persisting the incoming message raises
- **THEN** the task continues and the caller receives the agent's reply as if nothing had failed
- **AND** a warning with the traceback is logged

#### Scenario: No supervisor runtime is initialised

- **WHEN** the ingress runs without an initialised runtime, as in unit tests
- **THEN** persistence is skipped without raising
- **AND** the dispatch proceeds
