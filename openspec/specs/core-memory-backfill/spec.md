## Purpose

The one-time migration's contract for reading legacy blobs into the core-memory tables: which
sources it reads per storage backend, how the three durable sources collapse into the single
`memory` table, how it distinguishes an absent object from an unreadable one, what it does on
failure, and the guarantee that a failed run leaves no partial state behind.

## Requirements

### Requirement: The backfill reads legacy blobs through its own frozen readers

The one-time migration that creates the core-memory tables SHALL read legacy blob data through
readers defined inside the migration module, and SHALL NOT call `ouroboros.storage.blob`
factories, providers, or `DriveStorage`. The migration SHALL select its reader from the configured
storage backend: a boto3-based reader when the backend resolves to `s3`, and a `pathlib`-based
reader when it resolves to `file`. One reader SHALL serve all backfill sources, including the
identity document, so that a single client and a single backend resolution cover the whole run.

#### Scenario: S3 backend reads from the bucket

- **WHEN** the migration runs with the storage backend resolved to `s3`
- **THEN** legacy memory, scratchpad, knowledge, and identity blobs are read from the configured
  bucket
- **AND** no `StorageProvider` instance is constructed

#### Scenario: File backend reads from the data directory

- **WHEN** the migration runs with the storage backend resolved to `file`
- **THEN** legacy blobs are read from the data directory on disk
- **AND** the rows produced are identical to those the S3 reader produces from the same content

#### Scenario: Missing S3 configuration does not affect the file backend

- **WHEN** the storage backend resolves to `file` and no S3 bucket is configured
- **THEN** the migration completes normally

### Requirement: The backfill fills one memory table from three sources

The migration SHALL create the `memory` table and copy three legacy sources into it: the long-term
memory blobs under `memory/global/` with their manifest, the knowledge topic files under
`memory/knowledge/`, and the identity document at `memory/identity.md`. Each row SHALL carry the
kind implied by its source — the manifest or frontmatter type for long-term memories, `knowledge`
for topic files, and `identity` for the identity document. The migration SHALL NOT create
`memory_topics` or `knowledge_topics`. The scratchpad SHALL remain a separate table, backfilled as
before.

#### Scenario: Long-term memories keep their type as their kind

- **WHEN** the migration copies a memory whose manifest entry gives type `reference`
- **THEN** the resulting row has kind `reference`

#### Scenario: Identity becomes a row

- **WHEN** the migration runs with `memory/identity.md` present
- **THEN** a row of kind `identity` holds its content

#### Scenario: Absent identity seeds the default

- **WHEN** the migration runs with no identity document on the backend
- **THEN** a row of kind `identity` holding the default identity content is created

#### Scenario: Scratchpad remains its own table

- **WHEN** the migration completes
- **THEN** `scratchpad_blocks` exists with the active blocks copied into it
- **AND** no scratchpad block is written into the `memory` table

### Requirement: A name collision aborts the migration before anything is written

Because names are globally unique across kinds, the migration SHALL run a preflight that compares
the names it is about to copy from every source and SHALL abort when two sources claim the same
name. The abort SHALL name every conflicting pair in the log. The migration SHALL NOT resolve a
collision by renaming, suffixing, or dropping either side.

#### Scenario: A fact and a topic sharing a name abort the run

- **GIVEN** `memory/global/deploy.md` and `memory/knowledge/deploy.md` both exist
- **WHEN** the migration runs
- **THEN** it fails before inserting any row
- **AND** the log names the conflicting pair

#### Scenario: Distinct names complete normally

- **WHEN** no two sources claim the same name
- **THEN** the migration completes and every source row is copied

#### Scenario: No silent rename occurs

- **WHEN** a collision is detected
- **THEN** no row exists under a suffixed or otherwise altered name

### Requirement: An unreadable object aborts the migration

The migration SHALL distinguish an absent object from an object that exists but cannot be read. An
absent object SHALL be tolerated and contribute no row. Any other read failure — including
permission errors, encryption-key errors, throttling, transport errors, listing failures, and
malformed stored data — SHALL abort the migration by raising. The migration SHALL NOT substitute an
empty body, an empty listing, or a default value for content it failed to read.

#### Scenario: Absent object is tolerated

- **WHEN** a name referenced by the memory manifest has no corresponding blob
- **THEN** the migration continues
- **AND** no row is created for that name

#### Scenario: Unreadable object aborts

- **WHEN** fetching a memory topic blob fails with an error other than "not found"
- **THEN** the migration raises
- **AND** no rows from that migration run remain in the database

#### Scenario: Unreadable manifest aborts

- **WHEN** the memory manifest object exists but cannot be fetched or parsed
- **THEN** the migration raises
- **AND** the migration does not fall back to reconstructing rows from frontmatter alone

#### Scenario: Failed listing aborts

- **WHEN** listing the legacy memory prefix fails
- **THEN** the migration raises
- **AND** the application does not start with empty memory

#### Scenario: A fetch failure never yields an empty body

- **WHEN** the manifest names a topic and fetching that topic's body fails with a non-"not found" error
- **THEN** the migration raises
- **AND** no row is inserted carrying the manifest's metadata with empty content

### Requirement: A failed backfill leaves no partial state

A migration run that aborts SHALL leave neither inserted rows nor an advanced schema version, so
that restarting the application after the operator repairs the storage backend replays the backfill
from an empty starting state. The migration SHALL NOT provide a separate repair or resume path.

#### Scenario: Abort rolls back rows and version stamp together

- **WHEN** the backfill raises partway through inserting rows
- **THEN** the core-memory tables contain no rows from that run
- **AND** the recorded schema version does not include this migration

#### Scenario: Restart after repair migrates completely

- **WHEN** the migration has aborted once and the storage backend is subsequently repaired
- **THEN** the next start copies every legacy blob
- **AND** the result equals what a single successful run would have produced

### Requirement: Object key layout stays pinned to the shared key builder

The migration's S3 reader SHALL address objects at the same keys the shared key builder produces
for the same scope and name. A test SHALL assert this agreement, so that a later change to the
shared key builder fails loudly rather than causing the backfill to find nothing.

#### Scenario: Reader and key builder agree

- **WHEN** the migration's key for a memory topic is compared with `build_object_key` for the same
  scope and name
- **THEN** the two are equal

#### Scenario: Key drift is caught by tests

- **WHEN** the shared key builder's layout changes without a matching change in the migration
- **THEN** the pinning test fails
