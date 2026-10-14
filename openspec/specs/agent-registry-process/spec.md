# agent-registry-process Specification

## Purpose

The registry surface as a process of its own — the mode's fifth tier. It composes the registry, its
engine and its routes and nothing that runs a task, performs its peers' start-up refusals inline
because it never calls a model, binds what it was configured to bind, and answers for itself alone.
The single-process runtime mounts the same surface directly, beside the shared browser routes rather
than inside them, so the transport tier never serves a surface this tier owns.

## Requirements

### Requirement: The registry API is deployable as its own composition root

The registry surface SHALL be startable as a process of its own, composing the registry, its engine
and its routes, and composing nothing that runs a task: no supervisor loop, no agent runtime, no tool
catalogue, no message gateway.

This root SHALL be one application that includes the registry router and the owner settings router
under separate prefixes, over the one engine it composes; neither is reachable through the other's
prefix. The root's package is `multiuser_config/config_registry/`. They share this root because both answer questions about tables, neither calls a model,
and the settings surface verifies a named agent against the registry this root already holds — a
second root would compose the same engine and the same repository to serve one more table.

The root SHALL declare the tier it serves in its start-up log, alongside the database configuration
it composed and the paths it mounted, so a running process states what it is and what it answers for
rather than leaving either to be inferred from the environment it holds.

Nothing in this root SHALL import the supervisor or the agent runtime, statically or lazily. These
surfaces answer questions about tables; a dependency on what executes work would make them
unavailable whenever the runtime is.

Each surface holds its repositories over the engine this root composes, and the root SHALL make no
owner decision of its own: whether a request without the owner header is refused is
answered by the shared owner resolver from the mode declaration, per request. The root SHALL name no
fallback owner, no configured owner, and SHALL NOT read the mode declaration itself.

#### Scenario: The registry tier serves without a runtime

- **WHEN** the registry entrypoint composes its process
- **THEN** the registry routes are served
- **AND** no supervisor loop is started and no agent runtime is composed

#### Scenario: The tier serves both of its surfaces

- **WHEN** the registry entrypoint composes its process
- **THEN** both the registry surface and the owner settings surface are served, each on its own path

#### Scenario: The tier names itself and its mounts at start-up

- **WHEN** the registry entrypoint completes composition
- **THEN** it logs the tier it serves and the paths it mounted

#### Scenario: The root reaches no execution package

- **WHEN** this root's import graph is inspected
- **THEN** it reaches neither the supervisor nor the agent runtime

#### Scenario: The root makes no owner decision

- **WHEN** the root's source is inspected
- **THEN** it names no fallback owner, no configured owner, and does not read the mode declaration
- **AND** a request without the owner header is refused once the deployment is declared multi-user

### Requirement: The registry root makes every refusal its peers make, and no check of its own

This root SHALL perform the shared start-up's refusals in the shared start-up's order: the production
storage guard, the mode's refusals, the composed runtime store, the schema assertion, the deployment
keyspace comparison, and the deployment's configuration announcement.

The shared start-up's broker initialization SHALL NOT be among them. It describes the connection to the
broker a component composes its seams over, and this root composes no broker seam: taking the step
here would only make the registry's availability depend on a broker it never reads or writes. A
broker the configuration names is therefore neither described nor refused by this root, and the tier
starts whichever broker backend the deployment selects.

It SHALL NOT use the shared component start-up, because that start-up refuses to bring up a component
that has no usable model provider, and this tier never calls a model. It SHALL instead repeat those
steps inline, which is what the transport root already does for the same class of reason.

It SHALL add no refusal of its own. A tier that refused more than its peers is a fleet whose processes
disagree about what one configuration means; a refusal belonging to every component belongs in the
shared refusal set, not here.

#### Scenario: The tier starts without a model provider

- **WHEN** the registry entrypoint composes its process in an environment configuring no model provider
- **THEN** it starts and serves

#### Scenario: The mode's refusals are performed here too

- **WHEN** the registry entrypoint composes its process under a configuration the mode refuses
- **THEN** the process fails at start-up with the shared refusal's reason
- **AND** no route is served

#### Scenario: A schema below the build's refuses the start

- **WHEN** the registry entrypoint starts against a runtime database below the build's schema
- **THEN** it refuses to start and names the schema
- **AND** it does not bring the schema up itself

#### Scenario: The registry takes no broker step

- **WHEN** the registry entrypoint composes its process with the Kafka broker selected and no Kafka
  setting configured
- **THEN** it starts and serves
- **AND** it builds no Kafka client and its start-up names no broker initialization

### Requirement: The registry tier binds what it is configured to bind

The root SHALL bind the configured address or exit non-zero. It SHALL NOT scan for a free port, write
a port file, or terminate whatever holds the address it wanted: with instances sharing a network
namespace, each of those is a way to take a healthy peer down.

It SHALL answer a readiness probe reporting only what this process observes in itself, and that probe
SHALL carry no owner-bearing figure, because it is unauthenticated.

What this process observes in itself includes the runtime database. This root closes its synchronous
store after the schema assertion and serves its three surfaces from the asynchronous engine, so it
holds no synchronous pool for a probe to borrow and would have to keep one open for the probe alone
in any case. Readiness SHALL report the connection the probe holds for itself, which reaches the
same database this root just asserted the schema of. Liveness SHALL keep answering unconditionally —
this tier hosts no loop, and a database outage is not a process to replace.

#### Scenario: An unavailable address ends the process

- **WHEN** the configured address cannot be bound
- **THEN** the process exits non-zero naming the address
- **AND** no alternative port is chosen

#### Scenario: The probe answers for this process only

- **WHEN** the registry tier's readiness probe is requested
- **THEN** it reports this process's own readiness
- **AND** it carries no figure naming or aggregating any owner

#### Scenario: The database readiness reports is the one the probe holds

- **WHEN** the connection this root opened for the probe refuses
- **THEN** readiness answers not ready with a body naming nothing about the connection
- **AND** liveness answers success

### Requirement: The mode's tier enumeration and its deployment carry the registry

The registry tier SHALL appear in the enumeration of the mode's tiers together with the module that
starts it, and that module SHALL be importable and SHALL expose the callable a deployment invokes.

The deployment's stand SHALL start, wait for, and verify the registry's instances as it does every
other tier's, and the operator-facing process counts it reports SHALL name the number of processes it
actually waits for. A tier the stand does not wait for is a tier whose absence looks like health.

The address in front of the deployment SHALL route every path this tier serves — the registry's and
the owner settings surface's — to this tier's instances, and everything else to the transports, so
one address serves the whole deployment. A path this tier mounts and the address does not route is a
surface that exists in one deployment mode only.

#### Scenario: The registry tier is enumerated with a working entrypoint

- **WHEN** the enumeration of the mode's tiers is read
- **THEN** the registry tier is named with the module that starts it
- **AND** that module is importable and exposes the callable a deployment invokes

#### Scenario: The stand waits for the registry instances

- **WHEN** the deployment stand is brought up
- **THEN** it waits for and verifies the registry's instances alongside every other tier's
- **AND** the process count it reports equals the number of tier processes it waits for

#### Scenario: The address in front routes both of the tier's paths

- **WHEN** a request for a registry path or an owner settings path arrives at the deployment's single
  address
- **THEN** it reaches an instance of this tier
- **AND** a request for any other path reaches a transport instance

### Requirement: The single-process runtime serves the registry without a tier

The single-process runtime SHALL mount the same registry surface directly, so that the capability is
available in both deployment modes and the surface is defined once.

That mount SHALL sit beside the shared browser route collection rather than inside it. The collection
is what both the full runtime and the transport tier mount, and in the mode the registry surface
belongs to a different process — a collection that carried it would make the transport tier serve a
surface another tier owns.

#### Scenario: Both modes serve the same registry surface

- **WHEN** the registry surface served by the single-process runtime is compared with the one the
  registry tier serves
- **THEN** the two offer the same operations, built from the same definitions

#### Scenario: The transport tier does not serve the registry

- **WHEN** the route set the transport tier mounts is inspected
- **THEN** it contains no registry route
- **AND** it equals the shared browser route collection exactly
