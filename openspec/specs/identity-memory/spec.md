# identity-memory Specification

## Purpose

The agent's identity document: the single `memory` row of kind `identity`, holding no dedicated
table, port, or adapter pair of its own. Existing content is never rewritten implicitly, the
default is seeded only on absence, and the agent can never delete it — nor rewrite it, unless
identity self-edit is enabled (see `identity-self-edit`). The document
reaches the system prompt's stable partition, the startup verification check, and the A2A agent card
through the unified memory service. Identity's content, seeding, protection, and injection follow
the shared rules; see `unified-memory` and `memory-context-injection`. Because identity is a row,
`memory/identity.md` is no longer a read surface; see `data-tool-memory-paths`.

## Requirements
### Requirement: The identity document persists as a memory row

The system SHALL store the agent's identity document as the single `memory` row of kind `identity`
for the owner, named `identity`. The system SHALL NOT store the identity document as a blob on the
configured storage backend, and SHALL NOT maintain a dedicated identity table, port, or adapter
pair. Its content, seeding, protection, and injection follow the unified memory rules; see
`unified-memory` and `memory-context-injection`.

#### Scenario: Identity is a row, not an object

- **WHEN** the identity document is read
- **THEN** the content comes from the `memory` table
- **AND** no object or file at `memory/identity.md` is consulted

#### Scenario: Writing identity touches no blob storage

- **WHEN** an absent identity document is seeded on either storage backend
- **THEN** the default content is stored as a row
- **AND** no file is created under `{DATA_DIR}/memory/`

#### Scenario: A leftover identity blob is inert

- **GIVEN** `memory/identity.md` exists on the storage backend after the migration
- **WHEN** the identity document is read
- **THEN** the row is returned and the blob is neither read nor deleted

### Requirement: Identity content is never rewritten implicitly

The system SHALL return stored identity content verbatim and SHALL NOT overwrite, normalize,
version, or migrate a document that already exists. Seeding SHALL occur only when the row is absent.
No read SHALL write.

The only path that may replace an existing identity document SHALL be an explicit `memory_save` of
kind `identity`, and only while the identity self-edit setting is enabled; the rules that write obeys,
and the fact that no previous version is kept, are stated in `identity-self-edit`. While that setting
is off, no agent action SHALL be able to alter the document. At every setting value, no agent action
SHALL be able to remove it — the refusal itself is specified in `unified-memory`.

#### Scenario: Migrated content is returned unchanged

- **GIVEN** the identity row holds operator-written content that differs from the default
- **WHEN** the identity document is read
- **THEN** that exact content is returned

#### Scenario: Repeated reads perform no write

- **GIVEN** the identity row exists
- **WHEN** it is read several times
- **THEN** the stored content is unchanged after every read

#### Scenario: An empty document stays empty

- **GIVEN** the identity row exists and holds an empty string
- **WHEN** the identity document is read
- **THEN** an empty string is returned
- **AND** the default identity is not written

#### Scenario: The agent cannot overwrite identity while self-edit is off

- **WHEN** `memory_save` is called targeting the identity memory and the setting is off
- **THEN** the call is refused and the stored content is unchanged

### Requirement: The default identity is seeded only on absence

The system SHALL write the default identity content as a row and return it when, and only when, the
identity row has never been written. Seeding SHALL require no separate bootstrap step, and the
system SHALL NOT provide a bootstrap routine whose purpose is to pre-create identity documents.

#### Scenario: First read seeds the default

- **GIVEN** no identity row exists
- **WHEN** the identity document is read
- **THEN** the default identity content is returned
- **AND** a row of kind `identity` holding it is created

#### Scenario: Second read returns the stored document

- **GIVEN** a first read has seeded the default
- **WHEN** the identity document is read again
- **THEN** the stored content is returned without a further write

#### Scenario: Concurrent seeding converges

- **WHEN** two processes read an absent identity document at the same time
- **THEN** both receive the default identity content
- **AND** exactly one identity row exists afterwards

### Requirement: Identity is injected into the stable partition of the system prompt

The system SHALL include the identity document in the stable, cacheable partition of the assembled
system prompt, under its existing `## Identity` heading, on every prompt build and in every deploy
environment. The system SHALL obtain the content from the unified memory service rather than by
reading a file path. Identity SHALL be excluded from the rendered index, because its body is already
present in full.

#### Scenario: Stable partition carries identity

- **WHEN** the prompt is assembled
- **THEN** it contains a section headed `## Identity` whose body is the identity content
- **AND** that section is in a block declaring a cache breakpoint

#### Scenario: Every deploy environment carries identity

- **WHEN** the prompt is assembled under the production deploy environment
- **THEN** it contains the identity section with the identity content

#### Scenario: Identity does not appear in the index

- **WHEN** the memory index is rendered
- **THEN** no index entry names the identity memory

### Requirement: Startup verification checks identity through the service

The system SHALL determine identity health by asking the unified memory service for the identity
content rather than by probing the filesystem or blob storage. Identity SHALL be reported healthy
when the service returns non-empty content, and SHALL be reported as an issue when the service
returns empty content or raises.

#### Scenario: Identity with content is healthy

- **WHEN** startup verification runs and the memory service returns non-empty identity content
- **THEN** the identity check reports healthy
- **AND** no identity issue is counted

#### Scenario: Unreadable identity is an issue

- **WHEN** startup verification runs and the memory service raises
- **THEN** the identity check reports unhealthy
- **AND** an issue is counted and logged

#### Scenario: A database-only identity is healthy

- **GIVEN** the identity row exists and no local identity file does
- **WHEN** startup verification runs
- **THEN** the identity check reports healthy

### Requirement: The A2A agent card reads identity through the capability

The system SHALL derive the A2A agent card's name and description from the identity content obtained
through the unified memory service. The system SHALL NOT read the identity document from the
filesystem or from blob storage for this purpose.

#### Scenario: Agent card is populated from the row

- **GIVEN** the identity row holds content with a heading and a description
- **WHEN** the A2A agent card is built
- **THEN** the card's name and description are derived from that content

#### Scenario: Absent identity yields an empty card identity

- **GIVEN** the identity content cannot be read
- **WHEN** the A2A agent card is built
- **THEN** the card's name and description are empty
- **AND** card construction does not fail

### Requirement: No environment-profile document exists

The system SHALL NOT generate, store, read, or inject an environment-profile document. No
environment-profile section SHALL appear in the assembled system prompt, no environment-profile
check SHALL appear in startup verification, and no code path SHALL write such a document. Any
environment-profile object left over from an earlier version SHALL be ignored rather than read or
deleted.

#### Scenario: Fresh boot writes no environment profile

- **WHEN** the agent starts against an empty data root
- **THEN** no `memory/WORLD.md` object is created on any backend

#### Scenario: Prompt carries no environment-profile section

- **WHEN** the system prompt is assembled
- **THEN** it contains no `## Environment Profile` section

#### Scenario: A leftover environment profile is inert

- **GIVEN** `memory/WORLD.md` exists from an earlier version
- **WHEN** the system prompt is assembled and startup verification runs
- **THEN** its content appears in neither
- **AND** the object is not deleted

#### Scenario: Startup verification omits the environment-profile check

- **WHEN** startup verification runs
- **THEN** the emitted verification event contains no environment-profile check
- **AND** a missing environment profile contributes nothing to the issue count
