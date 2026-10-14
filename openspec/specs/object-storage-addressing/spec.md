# object-storage-addressing Specification

## Purpose

How durable object storage is reached and how its keys are built: the drive comes from the
caller's context rather than from a process-wide path, every call names the owner it acts for,
and the scopes declared owner-keyed carry owner and agent segments in the physical key.
## Requirements
### Requirement: Object storage is reached through an injected drive, never one resolved from a path

Code that reads or writes durable object storage SHALL do so through a `DriveStorage` or a
`StorageProvider` it was handed or built from configuration it holds, and SHALL NOT obtain one by
resolving a bare filesystem path against a process-wide data directory.

The generic data tools SHALL address the drive root carried by their `ToolContext`. A tool SHALL
NOT read the process's configured data directory to decide where its read or write lands, and SHALL
NOT import a private configuration helper from another module to do so.

A path SHALL remain the way a tool names an object *within* a drive, and SHALL NOT be the way it
chooses *which* drive. The two questions differ the moment a context's drive root and the process's
configured directory are not the same value.

#### Scenario: A tool reads through the drive its context names

- **WHEN** `data_read` runs under a `ToolContext` whose drive root is not the process's configured
  data directory
- **THEN** the object is read through a provider rooted at the context's drive root
- **AND** the process's configured data directory is not consulted

#### Scenario: A tool listing does not fall back to the configured directory

- **WHEN** `data_list` runs under a `ToolContext` whose drive root is not the configured data
  directory
- **THEN** the entries returned come from the context's drive root
- **AND** no listing is taken against the configured directory

#### Scenario: No tool reaches storage through the path-only resolver

- **WHEN** the modules under the tools package are inspected
- **THEN** none imports the drive-aware read, write or existence helpers that resolve a provider
  from a path
- **AND** none imports the configured-data-directory helper

### Requirement: Every object-storage call names the owner it acts for

Every method of the object-storage provider interface, of each of its backends, and of the drive
facade above them SHALL take an `OwnerScope` as an explicit, required argument. The argument SHALL
NOT be optional, SHALL NOT have a default, and SHALL NOT be reconstructed inside the layer from a
holder field, a module global, a filesystem path or an adjacent parameter.

The argument SHALL be named `owner_scope`, distinct from the existing `scope` argument, which keeps
its meaning as the storage scope. A signature SHALL carry both rather than overloading one name.

Work with an author SHALL pass that author's scope. Work with no author SHALL pass a service
identity widened by the existing one-way conversion, so that the widening is visible at the call
site that made the decision.

#### Scenario: The identity cannot be omitted

- **WHEN** an object-storage method is called without an owner scope
- **THEN** the call fails as a type error rather than defaulting to the instance's configured owner

#### Scenario: A tool call carries the task's owner

- **WHEN** a tool reads, writes, lists or deletes an object
- **THEN** the owner scope it passes is the one its `ToolContext` carries

#### Scenario: An ingress call carries the message's owner

- **WHEN** a transport stores or deletes an attachment for an arriving or departing message
- **THEN** the owner scope it passes comes from that message's scope, not from configuration

#### Scenario: Author-less work widens on purpose

- **WHEN** a startup check, a skill-state write, a runtime log append or a maintenance sweep reaches
  object storage
- **THEN** it passes a scope widened from a named service identity
- **AND** the job it names is a member of the closed job dictionary

### Requirement: Which scopes are owner-keyed is one named set, and the others are unchanged

The owner-keyed scopes SHALL be declared as a single named constant. A scope outside that set SHALL
produce the same key it produced before this change, for every owner scope, so that a
required-but-unused identity argument is never mistaken for isolation.

#### Scenario: A non-member scope ignores the owner

- **WHEN** a key is built for a scope outside the owner-keyed set under two different owner scopes
- **THEN** both keys are identical
- **AND** both equal the key produced before this change

#### Scenario: The set is declared in one place

- **WHEN** the storage layer is inspected
- **THEN** exactly one constant declares which scopes are owner-keyed
- **AND** no backend decides it independently

### Requirement: A generic write into a scope two owners would share is refused

The storage layer SHALL answer, for one scope, whether two owners writing one relative path into it
would land on one object. A generic per-caller write whose resolved path lands in such a scope SHALL
be refused, naming the scope and the scope to write to instead, so nothing turns a shared key into
somebody's content. A caller SHALL take that answer from the storage layer rather than reading the
mode itself.

Under the single-owner mode no scope is shared between owners, so the write SHALL be accepted and
the generic data tools SHALL keep the reach they had before owner keying existed.

#### Scenario: The refusal fires only where a collision is possible

- **WHEN** a generic write resolves into a scope outside the owner-keyed set under the multi-owner
  mode
- **THEN** it is refused, naming that scope
- **AND** the same write under the single-owner mode succeeds

### Requirement: The owner segment is injective, path-safe and legible

The segment SHALL be derived from the identity value by one shared function. It SHALL contain no
path separator, no traversal component and no character outside a conservative key alphabet, and
SHALL be bounded in length. Two different identity values SHALL NOT produce the same segment, even
when they differ only in characters the alphabet excludes. The segment SHALL retain a
human-recognisable rendering of the identity alongside whatever disambiguates it.

#### Scenario: A hostile owner id cannot escape its prefix

- **WHEN** a segment is built for an owner id containing slashes, `..`, whitespace or non-ASCII
- **THEN** the segment contains none of them
- **AND** the resulting key stays inside that owner's prefix

#### Scenario: Two ids that sanitize alike stay apart

- **WHEN** segments are built for two owner ids that differ only in excluded characters
- **THEN** the two segments differ

#### Scenario: A long owner id yields a bounded segment

- **WHEN** a segment is built for an owner id far longer than a filesystem path component allows
- **THEN** the segment length is bounded

### Requirement: A listing under an owner returns only that owner's keys

A key listing in an owner-keyed scope SHALL enumerate only the keys of the owner named by the call,
and SHALL return them in the logical form callers compare against a stored key, without the owner or
agent segment.

#### Scenario: One owner's listing omits another's objects

- **WHEN** two owners each hold objects under `uploads/chat/`
- **AND** one owner lists that prefix
- **THEN** only that owner's keys are returned

#### Scenario: A directory listing tool sees only its own owner

- **WHEN** a tool lists `uploads/chat` under one owner's context
- **THEN** the entries are that owner's attachment directories
- **AND** no other owner's directory name appears

#### Scenario: A listed key matches the stored form

- **WHEN** a key returned by a listing is compared with the stored key of the same attachment
- **THEN** the two are equal

#### Scenario: A tool listing does not fall back to the raw filesystem for a storage scope

- **WHEN** a listing under a storage scope returns nothing for the calling owner
- **THEN** the result reports an empty or absent listing
- **AND** no listing of the scope prefix without the owner segment is taken, so no owner segment
  name is disclosed

### Requirement: There is no migration from the pre-owner-segment layout

A deployment turning the multi-owner mode **on** SHALL start from a fresh volume or bucket. Under
that mode an object written before the owner segment existed SHALL NOT be read, listed or migrated:
no read SHALL retry a pre-change key, no listing SHALL consult one, and no rekey job SHALL exist.
A deployment that stays single-owner needs no migration at all, because its keys do not move.

#### Scenario: A pre-change object is not reachable under the mode

- **WHEN** the multi-owner mode is on
- **AND** an object exists only at its pre-owner-segment key
- **THEN** a read by its logical name returns nothing
- **AND** a listing under any owner returns none of them

#### Scenario: The mode is the one thing the layout branches on

- **WHEN** the key-building code is inspected
- **THEN** the multi-owner mode is the only setting it reads to decide whether a segment is built
- **AND** it is read in one place that the backend and the refusal share

### Requirement: Deployment-wide object maintenance runs once per owner

A maintenance sweep over an owner-keyed scope SHALL take an owner scope and act for that owner
alone, keeping its own cursor under that owner. Its caller SHALL enumerate the owners the runtime
database knows and SHALL run the sweep for each, so that no owner's expired objects are left behind
by a sweep that only ever saw the configured owner.

#### Scenario: Expired attachments of a second owner are removed

- **WHEN** two owners hold expired attachments and the maintenance loop runs
- **THEN** both owners' expired objects and metadata are removed

#### Scenario: Each owner keeps its own cursor

- **WHEN** the sweep is interrupted partway through one owner
- **THEN** resuming continues that owner from its own cursor
- **AND** another owner's cursor is unaffected

#### Scenario: The owner enumeration comes from stored rows

- **WHEN** the maintenance loop asks which owners to sweep
- **THEN** it reads the distinct ownership pairs recorded on stored message rows
- **AND** it does not read the instance's configured owner to answer

### Requirement: A call naming a retired storage scope is refused

The storage scopes SHALL be one declared set, and every scope in it SHALL be served by the shared
object-storage backend. A scope name the set once declared and no longer does SHALL be named as
retired, and a read, write or listing under it SHALL be refused naming that scope, and SHALL NOT
fall back to resolving the name against the answering instance's filesystem. A retired scope name is
the case that needs naming: a path under it looks like a storage address to every caller that
learned it, and a silent fallthrough serves bytes off one instance's disk under no owner segment, to
anyone on that instance.

The refusal SHALL be decided on the resolved target as well as on the name the caller wrote, so a
link inside the data directory cannot reach a retired scope under a name that is not one.

#### Scenario: A retired scope name is refused rather than read from disk

- **WHEN** a file exists under the instance's data directory in a retired scope's directory and a
  data tool names that path
- **THEN** the call is refused, naming the scope
- **AND** the file's bytes are not returned

#### Scenario: A retired scope name is refused on write

- **WHEN** a data tool writes to a path whose leading segment names a retired scope
- **THEN** the write is refused, naming the scope
- **AND** no file is created under the instance's data directory

#### Scenario: The refusal is decided on the resolved target

- **WHEN** a path inside a declared scope resolves, through a link, into a retired scope's directory
- **THEN** the call is refused on the resolved target, not on the name written

### Requirement: An owner-keyed scope puts the owner and the agent in the object key

For a storage scope declared owner-keyed, **and only under the multi-owner mode**, the physical
object key SHALL carry a segment for the owner and a segment for the agent, taken from the
`owner_scope` argument of the call and from nothing else. Under the single-owner mode every scope
SHALL produce the key it produced before owner keying existed, because one owner cannot collide with
itself and a deployment already holding objects at those keys SHALL keep reaching them.

The segments SHALL sit below the storage scope, so that the scope prefix an operator already
addresses stays the top level of the namespace and a data root remains recoverable from a physical
key.

The **logical** name — the storage scope plus the relative key that callers, provenance sidecars,
attachment records and tool results use — SHALL NOT change. The owner segment SHALL exist only in
the translation from `(owner_scope, scope, key)` to a physical key.

#### Scenario: The single-owner mode keys every scope the way it did before

- **WHEN** a key is built for any scope, `uploads` included, with the multi-owner mode off
- **THEN** the key is the tenant prefix, then the storage scope, then the relative key
- **AND** an object written at that key before owner keying existed is read and listed

#### Scenario: Two owners writing one relative path get two objects

- **WHEN** the multi-owner mode is on
- **AND** two tool contexts differing only in `owner_id` each write `uploads/report.pdf`
- **THEN** both objects exist
- **AND** each read returns the bytes its own owner wrote
- **AND** neither write overwrote the other

#### Scenario: The object key carries the owner below the scope

- **WHEN** an object key is built for the `uploads` scope under the multi-owner mode
- **THEN** the key is the tenant prefix, then the storage scope, then the owner segment, then the
  agent segment, then the relative key

#### Scenario: A path already handed to a model still resolves

- **WHEN** a tool result from before this change named `uploads/task_artifacts/{task_id}/{call}.txt`
  and the task reads it back afterwards
- **THEN** the read resolves, because the logical name is unchanged

### Requirement: Object storage is located only by configuration

Durable objects SHALL be stored only in the bucket named by `OUROBOROS_S3_BUCKET`. No setting, deploy
environment or mode SHALL select another place for them, including the answering instance's data
directory.

A process SHALL refuse to start when no bucket is configured, in every deploy environment and in
every mode, and the refusal SHALL name `OUROBOROS_S3_BUCKET`. A process that starts without a bucket
fails every later object operation one at a time, each failure looking like a broken upload,
attachment or file tool rather than missing configuration. The deploy environment SHALL NOT exempt a
process from this refusal: a development process has no other backend to fall back to, so an
exemption only moves the failure from start-up to the first attachment.

The refusal SHALL be made after the runtime-database refusal and before the deployment mode is
consulted, so every mode inherits one refusal instead of restating it. A mode SHALL NOT make a bucket
check of its own, because two checks of one setting are two places that can disagree about it.

A bucket refusal SHALL NOT render an endpoint, an access key or a secret key.

#### Scenario: A development process with no bucket refuses to start

- **WHEN** a process starts with `OUROBOROS_ENV=development`, the multi-user mode off, a runtime
  database the start-up refusal accepts, and no `OUROBOROS_S3_BUCKET`
- **THEN** it refuses to start, naming `OUROBOROS_S3_BUCKET`
- **AND** it serves no request

#### Scenario: A production process with no bucket refuses to start

- **WHEN** a process starts with `OUROBOROS_ENV=production` and no `OUROBOROS_S3_BUCKET`
- **THEN** it refuses to start, naming `OUROBOROS_S3_BUCKET`

#### Scenario: The runtime database is refused first

- **WHEN** a process starts with neither a runtime database URL nor a bucket configured
- **THEN** the refusal names the runtime database setting
- **AND** it does not name `OUROBOROS_S3_BUCKET`

#### Scenario: A configured bucket passes the refusal in every environment

- **WHEN** a process starts with a bucket configured, in either deploy environment
- **THEN** the bucket refusal does not fire

