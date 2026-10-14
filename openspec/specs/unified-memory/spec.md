# unified-memory Specification

## Purpose

One table, one service, one tool family for every durable named memory — long-term facts
and operational knowledge. Rows carry a kind drawn from a closed enum whose policy record
declares injection discipline and capacity; names are globally unique
across kinds and validated by rejection under a single grammar; descriptions are authored by the
caller rather than derived from the body. Writes are an overwrite or an append for every kind,
listing never reads a body, and callers obtain the service from exactly one factory. How these rows
reach the system prompt is `memory-context-injection`.
## Requirements
### Requirement: Durable named memory persists in one table

The system SHALL store every durable named memory — long-term facts and operational
knowledge — as one row in the runtime-database table `memory`. The system SHALL NOT maintain
`memory_topics`, `knowledge_topics`, or a blob-backed identity document as separate stores. Each row
SHALL carry its ownership pair `owner_id` and `agent_id`, its kind, name, authored description,
content, status, source, and the provenance timestamps `first_recorded_at`, `updated_at`,
`last_recalled_at`, and `recall_count`. The row SHALL NOT carry a per-row injection override flag;
injection discipline is determined by the row's kind policy alone. The row SHALL NOT carry a surface
or a chat: a memory belongs to an agent, not to the conversation it was learned in.

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

#### Scenario: A row names the agent that owns it

- **WHEN** the `memory` table is inspected
- **THEN** every row carries both `owner_id` and `agent_id`
- **AND** rows written before the axis existed carry the instance's configured agent

### Requirement: Names are globally unique across kinds

The system SHALL enforce uniqueness of `name` per owner **and agent** across all kinds, so that a
name identifies exactly one memory within one agent's namespace. Reading, deleting, or overwriting a
memory SHALL require only its name; no kind parameter SHALL be needed to disambiguate.

#### Scenario: A name taken by one kind cannot be reused by another

- **WHEN** a memory named `deploy` exists with kind `knowledge` and a save is attempted for `deploy`
  with kind `project`
- **THEN** the existing row is replaced rather than a second `deploy` created
- **AND** at most one row named `deploy` exists for the owner and agent

#### Scenario: Read needs no kind

- **WHEN** a memory is read by name alone
- **THEN** the matching row is returned regardless of its kind

#### Scenario: Two agents of one owner may hold the same name

- **WHEN** two scopes sharing an `owner_id` but differing in `agent_id` each save a memory under the
  same name
- **THEN** both rows exist and each scope reads back its own content
- **AND** neither scope's listing shows the other's row

### Requirement: Kind is a closed enum backed by a policy record

The system SHALL accept exactly the kinds `user`, `feedback`, `project`, `reference`, and
`knowledge`, and SHALL reject any other value. Each kind SHALL carry a declared policy stating
its injection discipline and capacity. All supported kinds SHALL be writable and deletable;
automated distillation SHALL continue to exclude user and feedback. Capacity limits and recall
type membership SHALL be read from that policy rather than hardcoded at their point of use.

A write and the capacity prune it triggers SHALL commit as a single transaction serialized by
`(owner_id, agent_id, kind)`. A save SHALL therefore commit its write together with its prune or commit
neither: a prune that fails SHALL NOT leave the write committed, and concurrent saves of the same
kind SHALL NOT drive the number of active memories of that kind above its declared capacity. The
serialization SHALL be scoped to `(owner_id, agent_id, kind)`, so saves of different kinds proceed
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
SHALL accept both for every supported kind.
Append SHALL join the new content to the existing body,
inserting a newline only when the existing body does not already end with one. Both modes SHALL read
(for append), persist, and prune the kind to capacity inside a single transaction serialized by
`(owner_id, agent_id, kind)`, so that concurrent writers of the same kind within one agent's
namespace serialize rather than race — an `overwrite` gains the same serialization guarantee as an
`append`. An unknown mode SHALL be refused.

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

#### Scenario: One agent's capacity prune leaves another's memories alone

- **WHEN** one agent fills a kind past its capacity while another agent of the same owner holds
  memories of that kind
- **THEN** only the writing agent's memories are archived
- **AND** the other agent's memories keep their `active` status

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

The service SHALL be stateless with respect to identity: it SHALL NOT be constructed with an owner,
and every one of its methods SHALL take the caller's `OwnerScope` and pass it to the port. A service
that bound an identity at construction would answer for whoever built the cached instance first.

#### Scenario: The service is built on first use

- **WHEN** the memory module is imported
- **THEN** no database engine is resolved until the service is first requested

#### Scenario: Clearing the cache rebinds to current settings

- **WHEN** the cached service is cleared and settings point at a different runtime database
- **THEN** the next call returns a service bound to the new database

#### Scenario: No sibling factories remain

- **WHEN** the core-memory package is inspected
- **THEN** it exposes no identity-service factory and no knowledge-service factory

#### Scenario: The service carries no identity of its own

- **WHEN** the memory service and its factory are inspected
- **THEN** neither takes an owner or an agent at construction
- **AND** every service method takes an `OwnerScope`

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

### Requirement: Every core-memory statement is addressed by an owner scope

The durable-memory I/O port SHALL take an `OwnerScope` on every method, and every statement it
issues SHALL filter on both ownership axes, `owner_id` and `agent_id`. No method SHALL accept a bare
owner string. The stored row SHALL NOT carry an owner of its own, so a row cannot name an ownership
the query disagrees with.

The scope's `surface_id` and `chat_id` SHALL NOT appear in any predicate. They travel with the scope
but do not partition memory: a fact learned on one surface or in one chat SHALL be readable from
every other.

#### Scenario: The port cannot be called without a scope

- **WHEN** the durable-memory port's methods are inspected
- **THEN** each takes an `OwnerScope` as its first argument
- **AND** none takes an `owner_id` parameter

#### Scenario: Surface and chat do not partition memory

- **WHEN** a memory saved under one scope is read under a scope differing only in `surface_id` and
  `chat_id`
- **THEN** the memory is found and its content is unchanged

### Requirement: Removed identity data is deleted without replacement

The runtime SHALL neither seed nor load a durable self-description. A new forward Alembic revision SHALL delete exactly rows in `memory` with kind `identity`, across all owners, agents, names, and statuses. It SHALL preserve every other row and column unchanged. An applied revision SHALL NOT be edited in a way that changes which rows a database ends up holding, and no revision SHALL re-seed identity; removing an applied revision's unreachable data-migration code is permitted, where unreachable means the revision can only run inside a `base → head` chain on a database that has no data for it to migrate. Downgrade SHALL NOT restore or fabricate deleted content. Architecture data-layout documentation SHALL record the deletion as irreversible and require old identity writers to be stopped before upgrade.

#### Scenario: Fresh runtime has no implicit memories

- **WHEN** storage, prompt assembly, and startup verification run on a fresh database
- **THEN** no memory is created implicitly and startup requires no identity document

#### Scenario: Upgrade preserves unrelated memories

- **WHEN** an existing database containing identity rows and other kinds is upgraded
- **THEN** only rows whose kind is `identity` are deleted, including archived and noncanonical-name rows
- **AND** other kinds remain byte-for-byte unchanged even if their name or content contains identity

#### Scenario: Downgrade does not resurrect deleted content

- **WHEN** the deletion revision is downgraded and upgraded again
- **THEN** removed rows remain absent and other memories remain unchanged

#### Scenario: A2A discovery needs no memory

- **WHEN** the incoming A2A server publishes its agent card
- **THEN** the configured name and description are used, with `ГигаАгент` and `Personal AI agent` as defaults
- **AND** no memory is loaded to build the card or start the server

#### Scenario: Removing an unreachable data migration leaves the end state unchanged

- **WHEN** a `base → head` chain runs on an empty database after an applied revision's unreachable
  legacy-source read has been removed from that revision
- **THEN** the resulting schema and rows are identical to those the chain produced before the removal
- **AND** no row with kind `identity` exists at head

