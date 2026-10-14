# owner-runtime-state Specification

## Purpose
Owner-scoped runtime state — claim, session id, spend counters — stored in the runtime DB and keyed by owner+agent.
## Requirements
### Requirement: A report about the runtime answers about the asker

A report an owner can ask for — a status command, a runtime-state endpoint — SHALL answer about that owner.
Every identifier and every number in it that belongs to an owner SHALL be the asker's: the owner id, the
session id, the queue counts, the spend counters, and any per-model or per-task breakdown of that spend.

A report SHALL NOT print another owner's identifiers, and SHALL NOT present a deployment-wide aggregate as
though it were the asker's figure. An aggregate over every owner's rows is not a smaller number's superset —
it is a different fact, and one owner cannot be told it is theirs.

A value that belongs to the instance rather than to any owner — how many worker lanes this process has, how
many are busy, how long it has been up, which build it runs — MAY appear in such a report, and SHALL be named
in the code as the instance's so that it is not later mistaken for an owner's.

Every read behind such a report SHALL take the asker's scope as an explicit argument. A helper that performs
one of these reads SHALL require the scope rather than defaulting it, so that the deployment-wide read cannot
be reintroduced by calling the helper without one.

#### Scenario: Two owners get different status

- **WHEN** two owners each ask for status on one instance configured with a third owner
- **THEN** each reply names its own asker's owner id and session id
- **AND** neither names the configured owner's

#### Scenario: Spend in a status reply is the asker's

- **WHEN** an owner asks for status while another owner's tasks have consumed model calls
- **THEN** the reported call and token counters cover only the asker's spend
- **AND** the per-model breakdown covers only the asker's calls

#### Scenario: The runtime-state endpoint counts the caller's work

- **WHEN** two owners request the runtime-state endpoint while each has tasks queued and running
- **THEN** each receives the queue and running counts of its own tasks and the spend of its own owner

#### Scenario: Instance-wide figures stay instance-wide and say so

- **WHEN** the runtime-state endpoint reports worker lanes
- **THEN** the totals describe this instance's lanes rather than the asker's share of them
- **AND** the code that produces them states that they belong to the instance

#### Scenario: A reporting read cannot be made without a scope

- **WHEN** the helpers behind these reports are inspected
- **THEN** each requires the owner scope as an argument with no default

### Requirement: The runtime state row belongs to an owner, not to the instance

The runtime state that records an owner's session and spend SHALL be keyed by the ownership pair,
one row per owner and agent. It SHALL NOT be keyed by a value that names the deployment, because
such a value is the same for everyone the instance serves and makes one person's session and spend
everyone's.

Reading it SHALL require the scope whose state is wanted. A reader with no owner in hand SHALL say
which owner it means rather than receiving whatever the instance last wrote.

A counter written into one owner's row SHALL be computed over that owner's rows. Where a counter is a
projection of another table, the aggregate that produces it SHALL carry the same ownership axes as the row it
fills — an unnarrowed aggregate written into an owner's row states the deployment's total as that owner's, and
is wrong even though the row it lands in is correctly keyed.

A row stamped with an owner's identity SHALL take every field of that identity from the same scope. A session
id read from the instance's own row and written onto another owner's record makes one row disagree with itself
about whose it is.

#### Scenario: Two owners hold separate runtime state

- **WHEN** two owners each run a task
- **THEN** each has its own row, its own session and its own counters
- **AND** neither reads the other's

#### Scenario: A read names the owner it wants

- **WHEN** the runtime state accessors are inspected
- **THEN** each takes the scope whose state it reads
- **AND** none derives an owner from configuration or from process state

#### Scenario: A counter is summed over its own owner's rows

- **WHEN** one owner's spend counters are refreshed while another owner has spend recorded on the same instance
- **THEN** the refreshed counters cover only the first owner's rows

#### Scenario: A usage row's session belongs to the same owner as its axes

- **WHEN** a model call made under one owner is recorded while the instance is configured with another
- **THEN** the stored row's session id is the recording owner's, not the configured owner's

### Requirement: An outbound message takes its identity from its own scope

The outbound door SHALL read the owner and the session for a message from the scope that message
carries, and SHALL NOT read them from process state. The scope is already required at that door and
already decides the chat; the remaining identity fields SHALL come from the same place, so that
nothing a message records can disagree with whose message it is.

#### Scenario: A message is recorded under its own owner

- **WHEN** messages for two owners are published in turn from one process
- **THEN** each stored row records the owner of the message that produced it

#### Scenario: The outbound door reads no process identity

- **WHEN** the outbound door is inspected
- **THEN** it derives no owner and no session from a stored instance row

### Requirement: Spend accumulates atomically, and last-writer fields say so

A counter in the runtime state row SHALL be advanced by a statement that adds to the stored value,
not by reading it, adding in the caller and writing the sum back. Two instances completing work for
one owner would otherwise each write a total computed from the same starting point, and one increment
would disappear with no error.

A field that is genuinely last-writer-wins SHALL be written as a plain assignment and SHALL be
documented as such where it is written, so that the difference between the two kinds of field is
visible rather than inferred.

No lock SHALL be retained beside these writes. A lock left next to an atomic statement is where the
next read-modify-write gets added.

#### Scenario: Concurrent spend does not lose an increment

- **WHEN** two processes record spend for one owner at the same time
- **THEN** the stored total reflects both

#### Scenario: The state row has no lock around it

- **WHEN** the runtime state module is inspected
- **THEN** no module-level lock guards reading and writing the row

### Requirement: Spend is attributed, and attribution is not a limit

Every record of model spend and every audit event SHALL carry the ownership axes of the work that
produced it, written at the time it is recorded rather than derived later by joining through a task —
work outside a task has no task to join through, and a deleted task takes its rows' attribution with
it.

Attribution SHALL NOT be presented as enforcement. Recording whose spend it was is not the same as
deciding what happens when an owner exceeds a limit, and this requirement covers only the first.

#### Scenario: Usage names its owner

- **WHEN** a model call is recorded
- **THEN** the row carries the ownership axes of the scope the call ran under

#### Scenario: Rows written before the axes existed keep their meaning

- **WHEN** rows recorded under the single-owner convention are read after the upgrade
- **THEN** they carry the legacy owner rather than an empty axis

