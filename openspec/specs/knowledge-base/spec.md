# knowledge-base Specification

## Purpose

Durable topic-keyed operational knowledge is no longer a store of its own: it is the `knowledge`
kind of the unified memory table. Persistence, naming, write modes, descriptions, concurrency,
absence reporting, and the service factory are all governed by `unified-memory`; index rendering and
prompt injection by `memory-context-injection`. What remains capability-specific is the one-time
copy of the pre-existing topic files under `memory/knowledge/` into that table. Topic paths under
`memory/knowledge/` are not a read surface; see `data-tool-memory-paths`.

## Requirements
### Requirement: Pre-existing knowledge files migrate into the database

The runtime-database migration SHALL copy each pre-existing topic file under `memory/knowledge/`
into the `memory` table as a row of kind `knowledge`, reading through the configured storage backend
rather than the local filesystem. It SHALL skip the derived index file, `_`-prefixed drafts, and any
filename that is not a valid memory name. Each copied row SHALL receive a description derived once
by a frozen copy of the retired summary heuristic, so no migrated topic reaches the index blank.
Every topic SHALL be copied as an ordinary `knowledge` row; no topic SHALL be privileged by name.
The source blobs SHALL be left in place as a rollback net.

#### Scenario: Topics are copied as knowledge memories

- **WHEN** the migration runs against a populated `memory/knowledge/` directory
- **THEN** each valid topic file becomes a `memory` row of kind `knowledge`
- **AND** its description is populated by the frozen heuristic

#### Scenario: No topic is privileged by name

- **WHEN** the directory contains a topic named `patterns`
- **THEN** its row is created as an ordinary `knowledge` memory with no special injection treatment
- **AND** it appears in the `## Knowledge` index like any other knowledge topic

#### Scenario: Derived and private files are skipped

- **WHEN** the directory contains `index-full.md` and `_draft.md`
- **THEN** neither becomes a row

#### Scenario: A name no tool could reach is skipped

- **WHEN** the directory contains a file whose stem is not a valid memory name
- **THEN** it is skipped rather than copied into a row nothing can read

#### Scenario: A missing directory is a no-op

- **WHEN** the migration runs with no `memory/knowledge/` directory present
- **THEN** it completes without creating rows and without failing

#### Scenario: An unreadable backend aborts the migration

- **WHEN** the storage backend cannot be reached or a payload cannot be read
- **THEN** the migration fails rather than completing with topics missing

#### Scenario: Source blobs survive the migration

- **WHEN** the migration completes
- **THEN** every source file under `memory/knowledge/` is still present on the backend
