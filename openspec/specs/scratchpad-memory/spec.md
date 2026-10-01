## Purpose

Durable block-structured working memory for the agent: scratchpad blocks live in the runtime
database (not blob storage), ordered by database-assigned identifiers, with FIFO capacity that
marks overflow as `evicted` rather than deleting rows. The rendered view is produced on demand;
callers obtain the service via a factory; existing agent read surfaces and a one-time file
migration keep continuity across the cutover.

## Requirements

### Requirement: Scratchpad blocks persist in the runtime database

The system SHALL store scratchpad working-memory blocks as rows in the runtime database table
`scratchpad_blocks`, each row carrying a database-assigned monotonically increasing `id` that
defines block order. The system SHALL NOT treat any file as the source of truth for scratchpad
blocks, and SHALL NOT derive block order from the block's displayed timestamp.

#### Scenario: Block is persisted as a row

- **WHEN** a scratchpad block is appended
- **THEN** a row exists in `scratchpad_blocks` with the block's content, source, ISO timestamp, and
  `status` of `active`

#### Scenario: Ordering follows assigned identifiers

- **WHEN** three blocks are appended in succession
- **THEN** their `id` values are strictly increasing in append order
- **AND** listing active blocks returns them oldest-first by `id`

#### Scenario: Order survives a timestamp that disagrees with position

- **WHEN** a block whose timestamp is newer than every existing block is inserted at the oldest
  position
- **THEN** listing active blocks returns it first
- **AND** the rendered view places it last

#### Scenario: Storage backend is irrelevant to block persistence

- **WHEN** the storage backend is configured as `s3`
- **THEN** appending and reading blocks uses the runtime database only
- **AND** no scratchpad block data is written to or read from blob storage

### Requirement: Appending a block is a single atomic operation

The system SHALL append a scratchpad block with a single insert statement whose order key is
assigned by the database. The system SHALL NOT compute the order key in application code, and SHALL
NOT coordinate that order-key assignment with file locks, process-level locks, or read-modify-write
retries. The system SHALL NOT branch its append implementation on the blob storage backend
(`local`, `s3`, or `memory`); dialect-specific SQL required by the runtime database engine is a
distinct axis and is permitted. The per-`owner_id` database serialization lock required by the
capacity requirement is not order-key coordination — it bounds the active-block count under
concurrency, not the order key — and does not violate this requirement.

#### Scenario: Order key comes from the database

- **WHEN** a block is appended
- **THEN** the insert statement does not supply an order key
- **AND** the persisted row has a database-assigned `id` greater than every existing block's

#### Scenario: Concurrent appends do not lose writes or collide

- **WHEN** two appends are issued concurrently
- **THEN** both blocks are persisted with distinct `id` values
- **AND** neither append overwrites the other or fails on a key conflict

### Requirement: Capacity overflow marks blocks evicted rather than deleting them

The system SHALL retain at most 10 blocks with `status` of `active` per owner. When an append
exceeds that cap, the system SHALL set `status` to `evicted` on the oldest active blocks so that
exactly 10 remain active. An append's insert and the eviction it triggers SHALL commit as a single
transaction serialized by `owner_id`: an append SHALL commit its insert together with its eviction
or commit neither, so a failed eviction SHALL NOT leave the insert committed, and concurrent appends
for the same owner SHALL NOT drive the number of active blocks of that owner above the cap, either
transiently or finally. The serialization SHALL be scoped to `owner_id`, so appends for different
owners proceed concurrently. The system SHALL NOT delete block rows and SHALL NOT write evicted block
bodies to a journal file.

#### Scenario: Eleventh block evicts the oldest

- **WHEN** an eleventh block is appended and ten active blocks already exist
- **THEN** the oldest block's `status` becomes `evicted`
- **AND** exactly ten blocks have `status` of `active`

#### Scenario: Concurrent same-owner appends do not exceed the cap

- **WHEN** more appends for one owner run concurrently than the cap, against a buffer already at the
  cap
- **THEN** after every append returns, exactly ten blocks have `status` of `active`
- **AND** every appended block is persisted, whether `active` or `evicted`, with none lost

#### Scenario: A failed eviction leaves neither the insert nor a partial evict

- **WHEN** the eviction statement of an append raises before the transaction commits
- **THEN** the block inserted by that same append is not committed
- **AND** the buffer is left at or below the cap, not above it

#### Scenario: Evicted content remains queryable

- **WHEN** a block has been evicted by capacity overflow
- **THEN** its row still exists with its original content, source, timestamp, and `id`
- **AND** it is excluded from the rendered scratchpad view

#### Scenario: No eviction journal file is written

- **WHEN** any number of blocks are evicted
- **THEN** no `memory/scratchpad_journal.jsonl` file is created or appended to

### Requirement: Scratchpad view is rendered on demand

The system SHALL render the scratchpad view from active rows at read time, newest-first, preserving
the existing text format: a `## Scratchpad (working memory — N/10 blocks)` header followed by one
`### [<timestamp> — <source>]` section per block. The system SHALL NOT persist the rendered view to
storage.

#### Scenario: Rendered text matches the established format

- **WHEN** the scratchpad view is rendered for a set of active blocks
- **THEN** the output is byte-identical to the format produced before this change for the same
  blocks

#### Scenario: Empty scratchpad renders default text

- **WHEN** the scratchpad view is rendered and no active blocks exist
- **THEN** the default empty-scratchpad text is returned
- **AND** no write occurs as a side effect of the read

#### Scenario: Rendering does not write files

- **WHEN** a block is appended or the view is rendered
- **THEN** no `memory/scratchpad.md` or `memory/scratchpad_blocks.json` file is written

### Requirement: The active block set is replaced transactionally

The system SHALL replace the active block set through the scratchpad service in a single
transaction: existing active blocks become `evicted` and the supplied replacement blocks are
inserted in the given order, taking fresh `id` values in that order. The system SHALL NOT write
block data using direct file descriptors or bypass the storage service.

This is the supported way to rewrite the buffer as a whole — reordering it, or collapsing several
blocks into one — as distinct from appending, which can only add to the end.

#### Scenario: A block placed first renders last

- **WHEN** the active set is replaced with a new block followed by the surviving blocks
- **THEN** the new block has a lower `id` than the surviving blocks
- **AND** the rendered view lists the surviving blocks before it, newest-first

#### Scenario: The previous active set is retained

- **WHEN** a replacement completes
- **THEN** the blocks that were active beforehand have `status` of `evicted`
- **AND** their content is unchanged

#### Scenario: Replacement survives on the S3 backend

- **WHEN** the active set is replaced with the storage backend configured as `s3`
- **THEN** the replacement set is returned by a subsequent read of active blocks

### Requirement: Scratchpad service is obtained from a factory

The system SHALL expose the scratchpad domain service through `get_scratchpad()`, which builds the
service on first use by resolving the runtime database engine from current settings and selecting
the adapter matching the engine dialect. The system SHALL NOT perform I/O at module import and
SHALL NOT expose a reset hook in business code for test isolation.

#### Scenario: Service is built on first use

- **WHEN** the scratchpad module is imported
- **THEN** no database connection is opened until `get_scratchpad()` is first called

#### Scenario: Service works against both supported dialects

- **WHEN** the runtime engine dialect is `postgresql` or `sqlite`
- **THEN** the factory returns a working service bound to that engine
- **AND** append, list, eviction, and replacement behave identically on both

#### Scenario: Callers do not construct storage directly

- **WHEN** any caller needs scratchpad access
- **THEN** it obtains the service via `get_scratchpad()` rather than instantiating an adapter or a
  blob-backed memory facade

### Requirement: Scratchpad remains readable through existing agent surfaces

The system SHALL continue to expose the scratchpad view through context injection and SHALL report
scratchpad availability at startup based on the service rather than file existence. Reads of
`scratchpad.md` via `data_read` SHALL NOT resolve through the scratchpad service: generic data
tools SHALL refuse the path and name `update_scratchpad`, as specified by
`data-tool-memory-paths`.

#### Scenario: data_read refuses and names the owning tool

- **WHEN** the agent reads `scratchpad.md` or `memory/scratchpad.md` via `data_read`
- **THEN** the call is refused with a message naming `update_scratchpad`
- **AND** the rendered scratchpad view is not returned

#### Scenario: Startup check reflects service availability

- **WHEN** the startup system-state check runs after cutover
- **THEN** the scratchpad check passes when the scratchpad service can read its table
- **AND** it does not fail merely because `memory/scratchpad.md` is absent

#### Scenario: Context injection is unchanged

- **WHEN** agent context is assembled
- **THEN** the scratchpad section appears with the same heading and body it had before this change

### Requirement: Pre-existing scratchpad files migrate into the database

The system SHALL, in the same Alembic migration that creates `scratchpad_blocks`, copy any existing
`memory/scratchpad_blocks.json` blocks into rows with `status` of `active` in array order. The
system SHALL NOT read `memory/scratchpad_journal.jsonl` and SHALL NOT create rows with `status` of
`evicted` during the backfill: pre-cutover eviction history is not carried into the database. A
failure to read existing data SHALL abort the migration, as specified by `core-memory-backfill`. A
missing file SHALL NOT abort the migration.

#### Scenario: Existing blocks are copied

- **WHEN** the migration runs against a deployment holding a populated `scratchpad_blocks.json`
- **THEN** one active row exists per block, preserving content, source, and timestamp
- **AND** their `id` order matches the original array order

#### Scenario: Eviction history is not imported

- **WHEN** the migration runs against a deployment holding a populated
  `memory/scratchpad_journal.jsonl`
- **THEN** the journal file is not read
- **AND** the backfill creates no rows with `status` of `evicted`

#### Scenario: Fresh deployment migrates cleanly

- **WHEN** the migration runs and no scratchpad files exist
- **THEN** the table is created and the migration succeeds with no rows inserted

#### Scenario: Unreadable backend aborts the migration

- **WHEN** `scratchpad_blocks.json` exists but cannot be read during the migration
- **THEN** the migration fails
- **AND** the application does not start with an empty scratchpad

#### Scenario: Legacy blobs are left in place

- **WHEN** the migration completes
- **THEN** `memory/scratchpad_blocks.json`, `memory/scratchpad.md`, and
  `memory/scratchpad_journal.jsonl` are unmodified on the storage backend
