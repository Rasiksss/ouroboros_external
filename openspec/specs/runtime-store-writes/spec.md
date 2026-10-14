# runtime-store-writes Specification

## Purpose
Conditional write patterns in the runtime store — lineage transitions, lease predicates, and schema-version guards.
## Requirements
### Requirement: A write that decides something decides it in the statement

A store write whose outcome depends on the row's current state SHALL express that dependency as a
predicate in the writing statement and SHALL learn what happened from the number of rows it
affected. It SHALL NOT read the row, decide in the caller, and write the decision, because the two
statements admit a writer between them and the decision is then made about a state that no longer
exists.

A write that applies nothing SHALL NOT be an error by default: the caller SHALL re-read and return
what is stored, so that the record a caller reports is the one that won rather than the one it read
first.

No lock SHALL stand in for this. A process-wide lock makes such a write look atomic to one process
and says nothing to a second, so a deployment that adds an instance loses the guarantee without any
code changing and without any failure being raised.

#### Scenario: A conditional write reports what it did

- **WHEN** a transition is attempted whose predicate the stored row does not satisfy
- **THEN** nothing is written
- **AND** the caller receives the stored record rather than the one it hoped to write

#### Scenario: Two writers race one row

- **WHEN** two processes attempt conflicting transitions on the same record at once
- **THEN** exactly one applies
- **AND** the other observes the applied state rather than overwriting it

### Requirement: A full-row write belongs to the row's author

A statement that rewrites every column SHALL be used only where the writer created the record. It
SHALL NOT be used to mutate a record another component owns, because a full-row write carries the
writer's whole snapshot and silently reverts every field it did not intend to touch.

#### Scenario: Full-row writes are confined to creation paths

- **WHEN** every caller of the full-row write is inspected
- **THEN** each is creating the record it writes
- **AND** no caller uses it to change the status of a record already in flight

### Requirement: The store has one instance, built by the composition root

The runtime store SHALL be constructed once, where the process composes its parts, and handed to the
components that use it. There SHALL NOT be a module-level accessor that lazily builds one on first
use: a lazy accessor read outside the lock that guards its own initialisation hands two callers two
different stores, after which every argument that begins "the store serialises this" is false even
within one process.

Use before composition SHALL be a loud failure rather than an absent value. Returning nothing is
what makes each call site invent its own fallback and become a second composition root.

#### Scenario: One store per process

- **WHEN** two components obtain the store during startup
- **THEN** both hold the same instance

#### Scenario: Use before composition fails loudly

- **WHEN** a component reaches for the store before the composition root built one
- **THEN** it raises, naming the wiring gap

### Requirement: Starting a runtime does not migrate the database

Schema upgrade SHALL NOT run as a side effect of constructing the store or of starting a runtime. It
SHALL be a deployment step that completes before any runtime starts, so that several instances
starting together do not race one another through the migrations.

A runtime SHALL check the schema version it requires and SHALL refuse to start against an older one,
naming the command that brings the schema up. A runtime that silently upgraded a database its peers
are already serving would change the schema under them.

#### Scenario: A runtime refuses an old schema

- **WHEN** a runtime starts against a database below the schema version it requires
- **THEN** it refuses to start and names the upgrade command
- **AND** it applies no migration

#### Scenario: Starting several runtimes migrates nothing

- **WHEN** several runtimes start against one prepared database
- **THEN** none of them runs a migration

### Requirement: A stopped requested child cannot be accepted afterwards

The transition that accepts a persisted `requested` child into the runnable queue SHALL be a
conditional database write whose predicate requires the row still to be `requested`. If a concurrent
tree stop changed it to `cancelled`, the acceptance SHALL report the stored cancelled row and SHALL
NOT restore a runnable status.

#### Scenario: Stop wins before stale subtask acceptance

- **WHEN** a supervisor read a requested child and another process cancels its tree before acceptance
- **THEN** the requested-to-scheduled write changes no row
- **AND** the child remains cancelled

### Requirement: The runtime database is located only by configuration

The runtime database SHALL be located only by the configured runtime database URL —
`OUROBOROS_RUNTIME_DB_URL` or the numbered failover keys the same resolution reads — and SHALL NOT be
derived from a filesystem path. There SHALL be no fallback that composes a database out of a data
directory, so that the data directory decides where files live and never which database answers.
A fallback of that shape is what turns every composition root into a second place where a database
can come into existence, none of which the deployment configured.

A process SHALL refuse to start when no runtime database URL is configured, and the refusal SHALL
name the setting that is missing rather than describing a connection failure. A configured URL that
names any dialect other than PostgreSQL SHALL be refused the same way.

The refusal SHALL be made before the deployment mode is consulted, so that every mode inherits one
refusal instead of restating it, and a mode SHALL NOT make a runtime database check of its own. Two
checks of one configuration are two places that can disagree about it.

A refusal about the runtime database SHALL NOT render a connection URL or any credential carried
inside one. It SHALL name the settings the runtime reads instead.

The tool that brings the schema up SHALL make the same refusal: invoked with no runtime database URL
configured, it SHALL refuse and name the missing setting, and SHALL NOT create a database of its own
and report success. A schema tool that succeeds against a database nothing else will open reports a
deployment step as done that has not been performed.

#### Scenario: A process with no configured database refuses to start

- **WHEN** a process starts with no runtime database URL configured and a data directory that exists
- **THEN** it refuses to start, naming the missing setting
- **AND** it composes no store and creates no database

#### Scenario: The schema tool refuses rather than creating a database

- **WHEN** the schema is brought up with no runtime database URL configured
- **THEN** the tool refuses, naming the missing setting
- **AND** no database is created and no success is reported

#### Scenario: A configured PostgreSQL URL is the only thing that decides which database answers

- **WHEN** a process starts with a configured PostgreSQL URL
- **THEN** startup proceeds against that database
- **AND** changing the data directory changes no part of which database answers

#### Scenario: A database refusal names settings rather than a connection string

- **WHEN** startup refuses the runtime database configuration
- **THEN** the refusal names the settings the runtime reads
- **AND** it renders no URL and no credential

