# dialogue-context-assembly Specification

## Purpose

What the agent's dialogue-related prompt context is assembled from: the owner message tail, read
straight from the runtime store by a read surface that takes an `OwnerScope` and a limit, and
nothing else — no cursor, no offset, no log name. There is no dialogue block file and no legacy
flat summary — the retired artifacts are neither read nor written, and their names do not appear
in read hints. Working memory is a separate channel; see `scratchpad-memory`.

## Requirements
### Requirement: Recent chat context is the owner message tail

The system SHALL build the `## Recent chat` prompt section from the most recent owner messages read
directly from the runtime message store. The system SHALL NOT consult a consolidation cursor,
watermark, offset, or generation signature when deciding which messages to include, and SHALL NOT
require any file on blob storage in order to produce the section.

The tail SHALL be one conversation's. It SHALL be bounded by the owner, the agent **and the chat** of
the scope the prompt is assembled for, and a message written under another chat of the same owner and
agent SHALL NOT appear in it. Bounding by the ownership pair alone is a cross-person exposure on the
default ingress rather than a wider view of one person's history: `transport/dialogs/transport.py::_scope_for_chat`
mints `configured_owner_id()` and `configured_agent_id()` for every peer of a
deployment, so peers differ only in the chat axis, and while that axis reaches no predicate one
peer's messages are in another peer's section.

Within that conversation the tail SHALL contain every message that belongs to conversation history,
whatever transport delivered it. A message delivered over A2A SHALL therefore appear in the tail of
the run whose scope carries that message's chat id — `transport/a2a/executor.py::derive_a2a_chat_id`
derives it from the remote task id, so that is the run serving that task and no
other — carrying the sender label that identifies its remote caller. Progress messages SHALL remain
excluded.

#### Scenario: Recent messages reach the prompt

- **WHEN** owner messages are appended to the runtime store under the scope a prompt is then assembled
  for
- **THEN** the `## Recent chat` section contains those messages
- **AND** messages appear oldest-first within the section

#### Scenario: A stray consolidation cursor file has no effect

- **WHEN** a `memory/dialogue_meta.json` file exists on the volume carrying a non-zero
  `last_consolidated_offset` and a `last_consolidated_message_id` naming an existing message
- **THEN** the `## Recent chat` section is identical to what it would be with no such file present
- **AND** no message is withheld on account of that file

#### Scenario: Progress stays out, another conversation stays out too

- **WHEN** the store holds progress messages of this chat, and non-progress messages written under a
  second chat of the same owner and agent, alongside this chat's own messages
- **THEN** the `## Recent chat` section contains this chat's non-progress messages and nothing else
- **AND** it contains no progress message and no message of the second chat

#### Scenario: Two dialogs peers do not share a section

- **WHEN** two peers of one dialogs deployment have both written, so their messages carry the same
  configured owner and agent and differ only in the chat axis, and a prompt is assembled for one
  peer's message
- **THEN** the section contains that peer's messages only
- **AND** no line of the other peer's conversation appears in it

#### Scenario: An A2A message is attributed to its caller in the prompt

- **WHEN** a prompt is assembled for an A2A task and that task's inbound message appears in the
  `## Recent chat` section
- **THEN** its rendered line carries the remote caller's sender label rather than the owner's default
  label

#### Scenario: Task ids from A2A messages reach working retrieval

- **WHEN** an A2A message in that task's own tail carries a `task_id` in its metadata
- **THEN** that task id is available to working retrieval as a recent task id

#### Scenario: Tail is bounded

- **WHEN** the store holds more messages for the scope's chat than the configured tail limit
- **THEN** the section contains at most the limit's worth of messages
- **AND** they are the most recent ones of that chat

### Requirement: Retired dialogue artifacts are neither read nor written

The system SHALL NOT read `memory/dialogue_blocks.json`, `memory/dialogue_summary.md`, or
`memory/dialogue_meta.json`, and SHALL NOT create, truncate, or otherwise write them. No prompt
section SHALL be derived from their contents.

#### Scenario: Existing block file is ignored

- **WHEN** a non-empty `memory/dialogue_blocks.json` is present on the volume and the prompt context
  is assembled
- **THEN** the assembled prompt contains no `## Dialogue History` section
- **AND** none of the file's content appears in the prompt

#### Scenario: Existing flat summary is ignored

- **WHEN** a non-empty `memory/dialogue_summary.md` is present on the volume and the prompt context
  is assembled
- **THEN** the assembled prompt contains no `## Legacy Dialogue Summary` section
- **AND** none of the file's content appears in the prompt

#### Scenario: Memory bootstrap creates no dialogue artifacts

- **WHEN** memory files are bootstrapped on an empty volume
- **THEN** no `dialogue_blocks.json`, `dialogue_summary.md`, or `dialogue_meta.json` exists under
  `memory/`

### Requirement: The owner-tail read surface takes a scope and a limit

The system SHALL expose the owner message tail through a single read operation parameterized by an
`OwnerScope` and a maximum message count, and nothing else. That operation SHALL NOT accept a log
name, an offset, or a cursor, SHALL bound its rows by the scope's owner, agent and chat axes, and
SHALL return canonical `ConversationMessage` values.

Narrowing the tail SHALL be a change of predicate, not of signature: the scope already carries the
chat axis, so `ouroboros/storage/chat_store.py::list_recent_owner_messages` keeps its
parameters and the statement it delegates to — `sql_runtime_store.py::list_chat_history_messages`,
whose ownership clause is `_OWNED` today — gains the third axis. No
chat-taking parameter SHALL be added beside the scope, because a second spelling of an axis the scope
already holds is a way for the two to disagree.

The prompt tail and the recent task ids working retrieval reads SHALL come from that one operation
(`ouroboros/agent/context.py`) and SHALL narrow together. A second, wider read SHALL
NOT be introduced to keep task ids cross-chat: a task id recovered from another conversation is how a
narrowed section regains, through retrieval, the content it was narrowed to exclude.

#### Scenario: Reading the tail

- **WHEN** the tail is requested with a scope and a limit of N
- **THEN** at most N messages of that owner, agent and chat are returned
- **AND** they are the most recent ones, ordered oldest-first

#### Scenario: Two conversations of one agent do not mix

- **WHEN** the tail is requested for one chat while the same owner and agent hold messages under
  another chat
- **THEN** no message of the other chat is returned
- **AND** the other chat's messages neither consume the limit nor displace the requested chat's oldest
  returned message

#### Scenario: Empty store

- **WHEN** the tail is requested and the store holds no messages for that owner, agent and chat
- **THEN** an empty result is returned
- **AND** no error is raised

#### Scenario: Task ids are recoverable from the tail

- **WHEN** a message in the tail carries a `task_id` in its metadata
- **THEN** that task id is available to working retrieval as a recent task id
- **AND** the recoverable set is the scope's chat's, because it is read through the same bounded
  operation

### Requirement: The committed summary is read under the same axes it was written

The `## Recent chat` section SHALL read the committed summary by the owner, agent and chat axes of the
scope the prompt is being assembled for, SHALL NOT read it by a constant key, and SHALL NOT read it by
the ownership pair alone once the tail below it is chat-bounded.

The two are one read in effect, not two: `ouroboros/agent/context.py` takes the summary and then
takes its `source_offsets_ref` as the cursor the fresh tail begins after. A summary selected by
a wider key than its tail would anchor one conversation's fresh tail on another conversation's
message — `sql_runtime_store.py::get_message` resolves a cursor by `message_id` alone, so a
foreign anchor is found rather than reported missing, and every message of this chat older than that
foreign row is silently cut from the section while the missing-cursor marker at
`ouroboros/agent/context.py` stays quiet. A conversation with no committed summary of its own
SHALL therefore show none, rather than falling back to a sibling conversation's.

#### Scenario: The section reads its own agent's summary

- **WHEN** summaries exist for two agents of one owner and a prompt is assembled for one of them
- **THEN** the section carries that agent's committed summary

#### Scenario: The section reads its own chat's summary

- **WHEN** two chats of one owner and agent each have a committed summary and a prompt is assembled
  for one of them
- **THEN** the section carries the summary written under that chat
- **AND** the fresh tail begins after that summary's own cursor

#### Scenario: A chat with no summary of its own shows none

- **WHEN** a prompt is assembled for a chat that has never been consolidated while a sibling chat of
  the same owner and agent has a committed summary
- **THEN** the section carries no committed summary block
- **AND** the fresh tail begins at that chat's oldest recoverable message rather than after the
  sibling's cursor

### Requirement: Retired artifact names are absent from read hints

The system SHALL NOT list `dialogue_blocks.json` or `dialogue_summary.md` among the memory artifact
names for which a repo read is redirected to a data read.

#### Scenario: Reading a retired artifact name gives no special hint

- **WHEN** a repo read is attempted for `dialogue_blocks.json` or `dialogue_summary.md`
- **THEN** the response is the ordinary not-found result for a missing repo file
- **AND** no hint pointing at `memory/` is emitted
