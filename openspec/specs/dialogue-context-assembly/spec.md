# dialogue-context-assembly Specification

## Purpose

What the agent's dialogue-related prompt context is assembled from: the owner message tail, read
straight from the runtime store by a read surface that takes only a limit. There is no
consolidation cursor, no dialogue block file, and no legacy flat summary — the retired artifacts
are neither read nor written, and their names do not appear in read hints. Working memory is a
separate channel; see `scratchpad-memory`.

## Requirements
### Requirement: Recent chat context is the owner message tail

The system SHALL build the `## Recent chat` prompt section from the most recent owner messages read
directly from the runtime message store. The system SHALL NOT consult a consolidation cursor,
watermark, offset, or generation signature when deciding which messages to include, and SHALL NOT
require any file on blob storage in order to produce the section.

The tail SHALL contain every message that belongs to conversation history, whatever transport
delivered it. Messages delivered over A2A SHALL therefore appear in the section, carrying the sender
label that identifies their remote caller. Progress messages SHALL remain excluded.

#### Scenario: Recent messages reach the prompt

- **WHEN** owner messages are appended to the runtime store and the prompt context is assembled
- **THEN** the `## Recent chat` section contains those messages
- **AND** messages appear oldest-first within the section

#### Scenario: A stray consolidation cursor file has no effect

- **WHEN** a `memory/dialogue_meta.json` file exists on the volume carrying a non-zero
  `last_consolidated_offset` and a `last_consolidated_message_id` naming an existing message
- **THEN** the `## Recent chat` section is identical to what it would be with no such file present
- **AND** no message is withheld on account of that file

#### Scenario: Progress stays out, A2A does not

- **WHEN** the store holds progress messages and messages with an A2A chat id alongside owner messages
- **THEN** the `## Recent chat` section contains the owner messages and the A2A messages
- **AND** it contains no progress message

#### Scenario: An A2A message is attributed to its caller in the prompt

- **WHEN** an A2A message appears in the `## Recent chat` section
- **THEN** its rendered line carries the remote caller's sender label rather than the owner's default
  label

#### Scenario: Task ids from A2A messages reach working retrieval

- **WHEN** an A2A message in the tail carries a `task_id` in its metadata
- **THEN** that task id is available to working retrieval as a recent task id

#### Scenario: Tail is bounded

- **WHEN** the store holds more owner messages than the configured tail limit
- **THEN** the section contains at most the limit's worth of messages
- **AND** they are the most recent ones

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

#### Scenario: Clearing owner chat history writes no dialogue artifacts

- **WHEN** owner chat history is cleared
- **THEN** owner messages are removed from the runtime store
- **AND** no `dialogue_blocks.json`, `dialogue_summary.md`, or `dialogue_meta.json` is created under
  `memory/`

#### Scenario: Memory bootstrap creates no dialogue artifacts

- **WHEN** memory files are bootstrapped on an empty volume
- **THEN** no `dialogue_blocks.json`, `dialogue_summary.md`, or `dialogue_meta.json` exists under
  `memory/`

### Requirement: The owner-tail read surface takes only a limit

The system SHALL expose the owner message tail through a single read operation parameterized by a
maximum message count and nothing else. That operation SHALL NOT accept a log name, an offset, or a
cursor, and SHALL return canonical `ConversationMessage` values.

#### Scenario: Reading the tail

- **WHEN** the tail is requested with a limit of N
- **THEN** at most N owner messages are returned
- **AND** they are the most recent ones, ordered oldest-first

#### Scenario: Empty store

- **WHEN** the tail is requested and the store holds no owner messages
- **THEN** an empty result is returned
- **AND** no error is raised

#### Scenario: Task ids are recoverable from the tail

- **WHEN** a message in the tail carries a `task_id` in its metadata
- **THEN** that task id is available to working retrieval as a recent task id

### Requirement: Retired artifact names are absent from read hints

The system SHALL NOT list `dialogue_blocks.json` or `dialogue_summary.md` among the memory artifact
names for which a repo read is redirected to a data read.

#### Scenario: Reading a retired artifact name gives no special hint

- **WHEN** a repo read is attempted for `dialogue_blocks.json` or `dialogue_summary.md`
- **THEN** the response is the ordinary not-found result for a missing repo file
- **AND** no hint pointing at `memory/` is emitted
