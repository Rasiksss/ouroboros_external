# unified-memory Specification

## Purpose

One table, one service, one tool family for every durable named memory — identity, long-term facts,
and operational knowledge alike. Rows carry a kind drawn from a closed enum whose policy record
declares writability, deletability, injection discipline, and capacity; names are globally unique
across kinds and validated by rejection under a single grammar; descriptions are authored by the
caller rather than derived from the body. Writes are an overwrite or an append for every kind,
listing never reads a body, and callers obtain the service from exactly one factory. How these rows
reach the system prompt is `memory-context-injection`; the one-time copy of the legacy stores into
this table is `core-memory-backfill`.

## Requirements
### Requirement: Durable named memory persists in one table

The system SHALL store every durable named memory — identity, long-term facts, and operational
knowledge alike — as one row in the runtime-database table `memory`. The system SHALL NOT maintain
`memory_topics`, `knowledge_topics`, or a blob-backed identity document as separate stores. Each row
SHALL carry its kind, name, authored description, content, status, source, and the provenance
timestamps `first_recorded_at`, `updated_at`, `last_recalled_at`, and `recall_count`. The row SHALL
NOT carry a per-row injection override flag; injection discipline is determined by the row's kind
policy alone. The production owner SHALL be `"global"`.

#### Scenario: A saved memory of any kind is retrievable

- **WHEN** a memory is saved with kind `project` and another with kind `knowledge`, then each is read
  back by name
- **THEN** both return their stored content unchanged
- **AND** both rows live in the `memory` table

#### Scenario: Provenance is recorded for every kind

- **WHEN** a `knowledge` memory is saved and then read
- **THEN** its `first_recorded_at` and `updated_at` are populated
- **AND** the same columns are populated for a `project` memory saved the same way

#### Scenario: No separate memory store remains

- **WHEN** the schema is inspected after migration
- **THEN** no `memory_topics` table and no `knowledge_topics` table exist

### Requirement: Names are globally unique across kinds

The system SHALL enforce uniqueness of `name` per owner across all kinds, so that a name identifies
exactly one memory. Reading, deleting, or overwriting a memory SHALL require only its name; no kind
parameter SHALL be needed to disambiguate.

#### Scenario: A name taken by one kind cannot be reused by another

- **WHEN** a memory named `deploy` exists with kind `knowledge` and a save is attempted for `deploy`
  with kind `project`
- **THEN** the existing row is replaced rather than a second `deploy` created
- **AND** at most one row named `deploy` exists for the owner

#### Scenario: Read needs no kind

- **WHEN** a memory is read by name alone
- **THEN** the matching row is returned regardless of its kind

### Requirement: Kind is a closed enum backed by a policy record

The system SHALL accept exactly the kinds `identity`, `user`, `feedback`, `project`, `reference`, and
`knowledge`, and SHALL reject any other value. Each kind SHALL carry a declared policy stating
whether it is writable through the tool surface, whether it is deletable, its injection discipline,
and its capacity. Capacity limits, recall type membership, and write restrictions SHALL be read from
that policy rather than hardcoded at their point of use.

A write and the capacity prune it triggers SHALL commit as a single transaction serialized by
`(owner_id, kind)`. A save SHALL therefore commit its write together with its prune or commit
neither: a prune that fails SHALL NOT leave the write committed, and concurrent saves of the same
kind SHALL NOT drive the number of active memories of that kind above its declared capacity. The
serialization SHALL be scoped to `(owner_id, kind)`, so saves of different kinds proceed
concurrently.

#### Scenario: An unknown kind is rejected

- **WHEN** a memory is saved with a kind outside the enum
- **THEN** the operation fails with an error naming the accepted kinds
- **AND** no row is written

#### Scenario: Capacity comes from the policy

- **WHEN** the number of active memories of a kind exceeds that kind's declared capacity
- **THEN** the oldest active memories of that kind are archived down to the limit
- **AND** memories of other kinds are unaffected

#### Scenario: Archiving does not delete

- **WHEN** a memory is archived by capacity pruning
- **THEN** its row remains with `status` of `archived`
- **AND** its content is still readable by name

#### Scenario: Concurrent same-kind saves do not exceed capacity

- **WHEN** more distinct new memories of one kind are saved concurrently than the kind's capacity
- **THEN** after every save returns, the number of active memories of that kind equals the capacity
- **AND** no memory that a concurrent save committed is dropped by another save's prune plan

#### Scenario: A failed prune leaves neither the write nor the archive

- **WHEN** a save's capacity prune raises before it commits
- **THEN** the write persisted by that same save is not committed
- **AND** the kind is left at or below its capacity, not above it

### Requirement: Identity is readable, never deletable, and writable only when self-edit is enabled

The system SHALL store the identity document as the single row of kind `identity`. `memory_delete`
SHALL refuse to delete it at every value of the identity self-edit setting. `memory_save` SHALL
refuse to write it while that setting is off, and SHALL accept a write while it is on, subject to
the identity write rules — fixed name, overwrite only, non-empty, size-capped — stated in
`identity-self-edit`. Both refusals SHALL follow the kind policy rather than a separate check.

What the setting changes — when `identity` is offered in the write tool's enum, what the prompt
says about it, and what a rewrite costs — is stated in `identity-self-edit` and SHALL NOT be
restated here. This spec states only the part that does not move: identity is one row, deletion
is refused at every value, and the write obeys the kind policy.

#### Scenario: Saving identity is refused while self-edit is off

- **WHEN** `memory_save` is called with kind `identity` and the setting is off
- **THEN** the call is refused with an error
- **AND** the stored identity content is unchanged

#### Scenario: Deleting identity is refused

- **WHEN** `memory_delete` is called for the identity memory, at either setting value
- **THEN** the call is refused with an error
- **AND** the row still exists

### Requirement: The accepted-kind enum is resolved per caller

The system SHALL build the `memory_save` tool schema's kind enum from the kinds writable by the
current caller, rather than from the storage enum. An automated distillation caller SHALL be offered
only `project`, `reference`, and `knowledge`. Domain-side rejection SHALL remain in place for any
kind that reaches the service despite not being advertised.

#### Scenario: Distillation is not offered user or feedback

- **WHEN** the tool schema is built for a distillation task
- **THEN** its kind enum contains neither `user` nor `feedback`

#### Scenario: Rejection still applies when the schema is ignored

- **WHEN** a distillation caller invokes `memory_save` with kind `feedback` regardless
- **THEN** the call is refused
- **AND** no row is written

### Requirement: Names are validated by rejection using one grammar

A memory name SHALL be accepted only if it is alphanumeric with interior underscores, hyphens, or
dots, at most 100 characters, and not a reserved name. An invalid name SHALL be rejected with an
error and SHALL NOT be silently rewritten into a different name. The error SHALL name a valid form
derived from the input, so a caller can retry without guessing.

#### Scenario: A name with a space is rejected, not coerced

- **WHEN** a memory is saved under the name `git recipes`
- **THEN** the operation fails with an error
- **AND** no memory named `git_recipes` or `git-recipes` is created

#### Scenario: The rejection suggests a valid form

- **WHEN** an invalid name is rejected
- **THEN** the error text contains a valid name derived from the input

#### Scenario: Dots are accepted

- **WHEN** a memory is saved under the name `browser.automation`
- **THEN** the name is accepted unchanged

#### Scenario: Reserved names are rejected

- **WHEN** a memory named `index-full` or `_index` is saved
- **THEN** the operation fails with a reserved-name error

### Requirement: Descriptions are authored, never derived

The system SHALL require a caller-supplied `description` on every write, for every kind, and SHALL
store it verbatim after whitespace normalization and truncation to 200 characters. The system SHALL
NOT derive a description from the body, and SHALL NOT retain the heuristic that produced knowledge
summaries from the first non-heading lines of the content.

#### Scenario: A write without a description is refused

- **WHEN** `memory_save` is called with an empty description
- **THEN** the call is refused
- **AND** no row is written

#### Scenario: The description is stored as written

- **WHEN** a memory is saved with a description and the index is rendered
- **THEN** the index line carries that description, not text extracted from the body

#### Scenario: Editing the body leaves the description alone

- **WHEN** an existing memory's content is overwritten with a new description supplied
- **THEN** the stored description is the newly supplied one
- **AND** it is not recomputed from the new body

### Requirement: Writes are either an overwrite or an append, for every kind

`memory_save` SHALL accept exactly the modes `overwrite` and `append`, defaulting to `overwrite`, and
SHALL accept both for every writable kind except where that kind's own rules restrict the mode —
today only `identity`, which is replaced whole and refuses `append` (see `identity-self-edit`).
Append SHALL join the new content to the existing body,
inserting a newline only when the existing body does not already end with one. Both modes SHALL read
(for append), persist, and prune the kind to capacity inside a single transaction serialized by
`(owner_id, kind)`, so that concurrent writers of the same kind serialize rather than race — an
`overwrite` gains the same serialization guarantee as an `append`. An unknown mode SHALL be refused.

#### Scenario: Append works on a fact, not only a knowledge memory

- **WHEN** content is appended to an existing memory of kind `project`
- **THEN** the stored body contains the previous content followed by the new content

#### Scenario: Append inserts one separator

- **WHEN** content is appended to a body that does not end with a newline
- **THEN** exactly one newline separates the old body from the new content

#### Scenario: Two concurrent appends both land

- **WHEN** two appends to the same memory run concurrently
- **THEN** the final content contains both fragments

#### Scenario: Append creates an absent memory without a lost update

- **WHEN** two concurrent appends both target a name that does not exist yet
- **THEN** the memory is created and neither fragment is dropped

#### Scenario: Overwrite persists and prunes in one transaction

- **WHEN** an `overwrite` save pushes its kind over capacity
- **THEN** the new content and the archival of the overflow commit together
- **AND** no state in which the write is visible but the overflow is not is observable to another reader

#### Scenario: Unknown mode is refused

- **WHEN** `memory_save` is called with a mode other than `overwrite` or `append`
- **THEN** the call is refused and nothing is written

### Requirement: Listing never reads a body

The persistence port SHALL expose a header listing that selects no `content` column, and `content`
SHALL appear in exactly one read statement — the single-row fetch. No index render, capacity check,
recall candidate listing, or tool listing SHALL cause bodies to be read from the store.

#### Scenario: Rendering the index reads no bodies

- **WHEN** the memory index is rendered with memories stored
- **THEN** no `content` column is selected

#### Scenario: Capacity pruning reads no bodies

- **WHEN** capacity pruning evaluates a kind that is over its limit
- **THEN** no `content` column is selected

#### Scenario: Fetch is the only body-bearing read

- **WHEN** the port's read statements are inspected
- **THEN** only the single-row fetch selects `content`

### Requirement: The memory service is obtained from a factory

Callers SHALL obtain the unified memory service from a module-level factory that wires the domain
service over the runtime database resolved from current settings. Nothing SHALL be built at import
time, and rebinding SHALL happen by clearing the factory's cache. There SHALL be exactly one such
factory; no separate identity or knowledge factory SHALL remain.

#### Scenario: The service is built on first use

- **WHEN** the memory module is imported
- **THEN** no database engine is resolved until the service is first requested

#### Scenario: Clearing the cache rebinds to current settings

- **WHEN** the cached service is cleared and settings point at a different runtime database
- **THEN** the next call returns a service bound to the new database

#### Scenario: No sibling factories remain

- **WHEN** the core-memory package is inspected
- **THEN** it exposes no identity-service factory and no knowledge-service factory

### Requirement: Four tools serve durable memory

The system SHALL expose exactly `memory_save`, `memory_read`, `memory_list`, and `memory_delete` for
durable named memory. The system SHALL NOT expose `knowledge_read`, `knowledge_write`, or
`knowledge_list`. `memory_list` SHALL accept an optional kind filter.

#### Scenario: Knowledge tools are gone

- **WHEN** the tool registry is enumerated
- **THEN** no tool named `knowledge_read`, `knowledge_write`, or `knowledge_list` is present

#### Scenario: A knowledge memory round-trips through the memory tools

- **WHEN** a memory of kind `knowledge` is written with `memory_save` and read with `memory_read`
- **THEN** the stored content is returned unchanged

#### Scenario: Listing can be filtered by kind

- **WHEN** `memory_list` is called with a kind filter
- **THEN** only memories of that kind are listed
