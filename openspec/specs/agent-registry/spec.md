# agent-registry Specification

## Purpose

Which agents an owner has. A durable record keyed on the owner together with the agent: one default
agent per owner, created on demand under the reserved single-agent identifier so no stored row is
re-keyed, and any number of further agents created, listed and deactivated by their owner. An agent
has no kind — the default agent is the row on the reserved id, and the primary key is what makes it
one per owner. The reserved id is a name and not a privilege: every agent an owner holds
deactivates on the same terms, and a boundary that resolves an absent agent header to that id
refuses it once the row behind it is inactive. The registry states absence and the HTTP surface
decides what absence means to a caller; the owner arrives in a header whose name is a constant of
the owner-scope contract and whose fallback the mode declaration decides, so the package names no
transport. It reads the runtime database through an asynchronous engine that fails over exactly as
the synchronous store does.
## Requirements
### Requirement: An owner's agents are a record, not a constant

The system SHALL keep a durable record of the agents belonging to each owner. Each record SHALL carry
an agent id, the owner it belongs to, a name, a description, and whether it is active. It SHALL NOT
carry a kind: the default agent is the one on the reserved id, so a type would be a second spelling
of the id that a writer could contradict.

An agent id SHALL be unique within its owner and SHALL NOT be assumed unique across owners. Every
lookup and every listing SHALL be keyed on the owner together with the agent, so no query can reach
an agent belonging to another owner.

That an owner has at most one default agent SHALL follow from the schema rather than from the code
that writes to it: the primary key on the owner together with the agent admits one row on the
reserved id, so a writer that bypasses the repository cannot record a second.

#### Scenario: An agent belongs to exactly one owner

- **WHEN** two owners each hold an agent recorded under the same agent id
- **THEN** each owner's lookup returns its own agent
- **AND** neither owner's listing contains the other's

#### Scenario: The record carries no kind

- **WHEN** the table definition is inspected
- **THEN** it holds no agent-type column
- **AND** the primary key on the owner together with the agent admits one row on the reserved id

### Requirement: The default agent is created on demand and carries the reserved id

Every owner SHALL have exactly one record on the reserved id. It SHALL come into existence on first
use rather than by a seeding step, so an owner that appears for the first time is served without an
administrative action. Whether that record is active is the owner's to decide afterwards; the
guarantee is that there is never a second one.

The default agent's id SHALL be the reserved single-agent identifier that the instance's
configuration already mints. Every scope-keyed row written before this capability existed carries
that value, so the record is additive and no stored row is migrated or re-keyed.

The reserved id names the record an absent agent header resolves to, and nothing beyond that. It
SHALL carry no rule the owner's other agents do not carry.

Creation SHALL be atomic against concurrent callers. Several processes resolving the same new owner
at the same moment SHALL result in exactly one default agent, and every one of those callers SHALL
receive it. A read followed by a conditional write SHALL NOT be used, because the deployment runs
several instances against one table.

Creation on demand SHALL NOT revive a deactivated record. Resolving an owner whose reserved-id
record is inactive SHALL return it inactive and write no row: an owner who retired that agent
retired it, and a resolution that silently re-activates it is the deactivation failing quietly.

#### Scenario: A previously unseen owner receives a default agent

- **WHEN** the default agent is requested for an owner that has no record
- **THEN** a default agent is created for that owner and returned
- **AND** its id equals the reserved single-agent identifier

#### Scenario: Concurrent first requests produce one default agent

- **WHEN** several callers request the default agent for the same previously unseen owner at once
- **THEN** exactly one default agent row exists for that owner afterwards
- **AND** every caller receives that same agent

#### Scenario: A second request returns the existing default agent

- **WHEN** the default agent is requested for an owner that already has one
- **THEN** the existing agent is returned and no row is written

#### Scenario: A deactivated default agent is not revived by resolution

- **WHEN** the default agent is requested for an owner whose reserved-id record is inactive
- **THEN** that record is returned with its active flag still false
- **AND** no row is written

### Requirement: Agents are created, listed, and deactivated

An owner SHALL be able to create an agent with a name and a description. Its id SHALL be generated
by the system and SHALL NOT be supplied by the caller, so no caller can choose an id that collides
with the reserved default-agent identifier or with an existing agent.

Listing SHALL return the agents of the requesting owner. Deactivation SHALL mark an agent inactive
rather than deleting its row, because the agent axis keys stored rows that outlive the agent. The
repository's deactivation SHALL state whether it marked a row, so an absent agent is a not-found
response rather than a silent no-op without a read placed in front of the write; the surface answers
a successful deactivation with no content.

Every agent an owner holds SHALL be deactivatable on the same terms, the one on the reserved id
included. That id is a name and not a privilege: it is what an absent agent header resolves to and
what every scope-keyed row written before this capability existed carries, and neither of those
makes the record behind it a different kind of thing. No agent id SHALL be inspected before the row
is touched, and the surface SHALL offer no conflict response for any of them.

#### Scenario: A created agent appears in the owner's listing

- **WHEN** an owner creates an agent
- **THEN** the created agent is returned with a system-generated id
- **AND** it appears in that owner's listing

#### Scenario: A caller cannot choose an agent id

- **WHEN** a creation request carries an agent id
- **THEN** the supplied id is not used

#### Scenario: Deactivation preserves the row

- **WHEN** an owner deactivates one of its agents
- **THEN** the agent is marked inactive and its row still exists
- **AND** rows previously written under that agent are unaffected
- **AND** the response carries no body

#### Scenario: Deactivating an absent agent is stated rather than silent

- **WHEN** an owner deactivates an agent its record does not hold
- **THEN** the repository states that no row was marked
- **AND** the surface answers not found without having read the agent first

#### Scenario: The agent on the reserved id deactivates like any other

- **WHEN** an owner deactivates the agent recorded under the reserved id
- **THEN** it is marked inactive and its row still exists
- **AND** the response carries no body, exactly as for any other agent

#### Scenario: No agent id is refused as a conflict

- **WHEN** the deactivation route is exercised over every agent an owner holds
- **THEN** no request is answered with a conflict status

### Requirement: Absence is stated by the repository and interpreted by the surface

A lookup for an agent that does not exist, or that exists under another owner, SHALL return an
absence value rather than raising and rather than substituting another agent. The HTTP surface SHALL
translate that absence into a not-found response.

The layering is the point: the repository states what the table holds, and the surface decides what
holding nothing means to a caller. A repository that raised would make every consumer catch a
transport concern.

#### Scenario: A lookup for an unknown agent returns absence

- **WHEN** an agent id that no row carries is looked up for an owner
- **THEN** the repository returns the absence value

#### Scenario: A lookup across owners returns absence

- **WHEN** an owner looks up an agent id that exists under a different owner
- **THEN** the repository returns the absence value
- **AND** the other owner's agent is not returned

#### Scenario: The surface answers not-found

- **WHEN** a request names an agent the repository reports absent
- **THEN** the response is a not-found status
- **AND** no other owner's record is disclosed in it

### Requirement: The registry is served over HTTP with a generated API document

The registry SHALL expose an HTTP surface offering listing, creation, and deactivation of the
requesting owner's agents. The surface SHALL publish a machine-readable API document generated from
the same definitions that serve the requests, so the document cannot describe a surface that is not
the one running.

The surface SHALL establish the deployment's operation identifier on every request, accepting the
caller's when the request carries one and originating one when it does not. A surface mounted as a
sub-application does not inherit the enclosing application's request instrumentation, so it SHALL
install its own.

#### Scenario: The API document describes the served routes

- **WHEN** the generated API document is fetched
- **THEN** it describes the listing, creation, and deactivation operations
- **AND** every operation it describes is served

#### Scenario: Every response carries the operation identifier

- **WHEN** a request reaches the registry surface without an operation identifier
- **THEN** one is originated for that request
- **AND** a request that carries a well-formed one has it accepted rather than replaced

### Requirement: The owner is taken from a header, and the fallback is the mode's

The registry surface SHALL read the owner from a named request header. The header's name is a
constant of the owner-scope contract, read by the transport and by the one owner resolver every
owner-bearing surface installs, so the registry package depends on no user-facing transport.

Whether a missing header is refused SHALL be decided by the mode declaration, read by that resolver
per request: while the deployment is not declared multi-user a missing or empty header resolves to
the instance's configured owner, so a single-owner deployment is not made to present a header naming
the only owner it has; once the deployment is declared multi-user a missing or empty header SHALL be
refused with a forbidden response. No composition root SHALL hand the surface a fallback owner.

The surface SHALL be a router whose module holds its registry repository, over the shared engine
holder, so a root has nothing to initialise on it; each root includes it on the application it
builds and installs the request instrumentation there.

#### Scenario: A missing header is refused in the multi-user mode

- **WHEN** the deployment is declared multi-user and a request arrives without the header
- **THEN** the request is refused with a forbidden response
- **AND** no row is read or written

#### Scenario: The configured owner serves the single-owner deployment

- **WHEN** the deployment is not declared multi-user and a request arrives without the header
- **THEN** the request is served under the configured owner

#### Scenario: The header selects the owner where one is supplied

- **WHEN** requests arrive carrying different owner headers
- **THEN** each is served the agents of the owner its own header named

#### Scenario: The registry package names no transport

- **WHEN** the registry package's imports are inspected
- **THEN** none of them reaches a user-facing transport package

### Requirement: The registry reaches the runtime database asynchronously over the configured PostgreSQL URLs

The registry SHALL use an asynchronous engine over the same runtime database the rest of the system
uses, composed from the same configured URLs.

Where the deployment configures several database URLs, the registry's engine SHALL fail over across
them with the same rotation and the same preference for the last known-good URL that the synchronous
store applies. A registry pinned to one URL while the synchronous store fails over would take an
owner-facing surface down on a node loss that the rest of the system survives.

No connection URL, and no credential within one, SHALL appear in an error message raised by the
engine holder.

#### Scenario: Failover moves to a healthy URL

- **WHEN** several database URLs are configured and the first is unreachable
- **THEN** the registry connects through another configured URL
- **AND** later connections prefer the one that last succeeded

#### Scenario: A failure names no credential

- **WHEN** the engine holder raises because it was asked for an engine it cannot provide
- **THEN** the message contains no connection URL and no credential

