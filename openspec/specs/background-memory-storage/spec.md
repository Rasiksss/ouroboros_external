# background-memory-storage Specification

## Purpose
TBD - created by archiving change give-background-memory-its-own-repositories. Update Purpose after archive.
## Requirements
### Requirement: The package reaches the runtime database only through repositories of its own

Every read and every write the background-memory package makes against the runtime database SHALL go
through a repository declared inside the package. No module of the package SHALL hold, be handed, or
import the runtime store, and no handler SHALL receive a store-shaped collaborator that forwards to
one.

A repository SHALL be constructed by a composition root with the asynchronous engine as a required
constructor argument, with no default and no module-level accessor that builds one lazily. A
repository SHALL read no setting: what database it addresses is what the root handed it.

Duplicating by meaning a statement `ouroboros/storage` also issues SHALL be allowed. The package owns
the queries its own work needs, and a query that answers a question only this package asks SHALL live
here rather than be widened in a module another component owns.

#### Scenario: No module of the package names the store

- **WHEN** every module of the package is parsed and its imports and constructor arguments inspected
- **THEN** none imports the runtime store, names its type, or takes it as an argument

#### Scenario: A repository is handed its engine

- **WHEN** a repository of this package is constructed
- **THEN** it requires the asynchronous engine as an argument
- **AND** constructing it without one raises rather than resolving an engine itself

#### Scenario: A duplicated query is not a violation

- **WHEN** a repository issues a statement that answers the same question as one in `ouroboros/storage`
- **THEN** that is accepted, and neither module is changed to serve the other

### Requirement: A repository of this package never blocks the event loop

Every repository method SHALL be a coroutine and SHALL reach the database through the asynchronous
engine: `connect()` for a read, `begin()` for a write, each as an asynchronous context manager. No
method SHALL open a synchronous session, call a synchronous engine, or move the work to a thread.

A write that decides something SHALL decide it in the statement, as the runtime store's writes
already do: acquiring the lease and committing the summary under compare-and-swap SHALL remain
single statements whose predicate the database evaluates, never a read followed by a write.

#### Scenario: Every repository method is awaitable

- **WHEN** the repository modules are parsed
- **THEN** every public method is declared as a coroutine

#### Scenario: A repository opens no synchronous connection

- **WHEN** the repository modules are parsed
- **THEN** none constructs a synchronous engine, session or connection, and none dispatches to a thread

#### Scenario: Acquiring a lease stays one statement

- **WHEN** two instances acquire the same pair's lease concurrently through these repositories
- **THEN** exactly one holds it afterwards
- **AND** the decision was made by the statement, not by a read the caller performed first

### Requirement: An encrypted field is decrypted on every dialect

A repository that returns message text SHALL decrypt it whatever dialect the engine speaks.
Encryption is gated on PostgreSQL, but a stored value is recognised by its prefix rather than by the
dialect, so a repository that decrypts only on PostgreSQL returns the ciphertext as if it were text
and reports no error while doing so.

The fate of a row whose key is unavailable SHALL be declared rather than inherited: that failure is
not a database error and is not covered by the store's failure translation, so a repository SHALL
state whether such a row is skipped, surfaced as an error, or returned marked, and SHALL NOT let it
pass as ordinary text.

#### Scenario: A message decrypts on a non-PostgreSQL dialect

- **WHEN** a repository reads a stored message whose text carries the encryption prefix, on an engine
  that is not PostgreSQL
- **THEN** it returns the plain text
- **AND** it does not return the prefixed ciphertext

#### Scenario: An unreadable field takes the declared path

- **WHEN** the key needed to read a stored field is unavailable
- **THEN** the repository takes the path it declares for that case
- **AND** the row is not returned as though its text had been read

### Requirement: A predicate that narrowed a query travels with the query

Where the statement a repository replaces was reached through a layer that added a predicate of its
own, the new statement SHALL carry that predicate. A query lifted from the adapter below such a layer
answers a wider question than its caller ever asked, and the difference is a row set, not an error.

The listing of prompt-bearing memory headers SHALL therefore keep its status filter, which today
exists only on the domain service above the SQL adapter.

#### Scenario: Prompt headers exclude what the status filter excluded

- **WHEN** memory rows exist in the statuses the prompt listing excludes
- **THEN** the repository's listing omits them
- **AND** it returns the same set the domain service returned for the same scope

### Requirement: The tier composes the engine its repositories need

A composition root that builds these repositories SHALL compose the asynchronous engine before it
builds them, and SHALL dispose it when the process stops. The background-memory tier composes no such
engine today, so its first repository call would raise rather than degrade.

The engine's pool SHALL be sized for what this tier actually runs concurrently rather than left at the
default, because a pool smaller than the deliveries in flight turns into an unnamed queue in front of
the database with a timeout the caller did not choose.

#### Scenario: The tier starts with an engine composed

- **WHEN** the background-memory tier finishes composing itself
- **THEN** the asynchronous engine is composed, and a repository call succeeds rather than raising
  that no engine is composed

#### Scenario: The tier disposes its engine

- **WHEN** the tier stops
- **THEN** the engine it composed is disposed

### Requirement: A statement that moved leaves its old home

A statement this package takes over, and which no other component calls in production, SHALL be
removed from the module it moved out of and from the protocol that declared it. Two implementations
of one decision are two behaviours the moment one is edited.

A statement another component still calls SHALL stay where it is, and the package SHALL issue its own
rather than widening the caller's.

#### Scenario: The lease statements have one home

- **WHEN** the runtime store and its protocol are inspected after the move
- **THEN** neither declares a method that reads, acquires, renews or releases the consolidation lease
- **AND** the package's repository is the only place those statements exist

#### Scenario: A statement with another caller stays

- **WHEN** a statement this package also needs is still called by another component
- **THEN** it remains in that component
- **AND** the package issues its own statement rather than that one being changed to serve both

