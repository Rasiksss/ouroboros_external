# tier-observability Specification

## Purpose
Define process monitoring, startup configuration announcements and owner-scoped tracing.
## Requirements
### Requirement: Every process of a deployment initializes its own process monitoring

Each composition root SHALL initialize infrastructure monitoring for its own process, on the same
settings that govern it in the single-process runtime, and SHALL shut it down on the process's way
out. This holds for every tier of the multi-user mode as it holds for the single-process
runtime: monitoring attaches to a process, so a deployment is monitored only if each of its
processes initializes it.

The initialization SHALL live in the shared component start-up for the tiers that use it, and SHALL
be made explicitly by each composition root that composes inline and uses no shared start-up. There
is more than one such root — the browser transport and the agent registry both compose inline — and
each of them SHALL make the call itself. A monitoring initialization only one root performs is not
monitoring of the deployment.

An initialization that fails SHALL NOT stop the process: monitoring reports on the runtime and is not
part of it.

#### Scenario: A tier that uses the shared start-up is monitored

- **WHEN** the supervisor, agent-runtime or background-memory tier starts with monitoring enabled
- **THEN** monitoring is initialized for that process before it composes anything of its own

#### Scenario: A root that composes inline is monitored too

- **WHEN** the browser transport tier or the agent registry tier starts with monitoring enabled
- **THEN** monitoring is initialized for that process, by its own root rather than by the shared start-up

#### Scenario: Each process releases monitoring on the way out

- **WHEN** any tier process is stopping, whether it returned or its loop failed
- **THEN** monitoring is shut down for that process before it exits

#### Scenario: Monitoring stays off when it is not configured

- **WHEN** a tier starts with none of the monitoring settings set
- **THEN** no monitoring is initialized and the tier starts normally

#### Scenario: A monitoring failure does not stop a tier

- **WHEN** the monitoring initialization raises in any tier
- **THEN** the failure is logged and the tier continues its start-up

### Requirement: A deployment announces its agent system configuration on every agent process start

Every server or agent-runtime process start SHALL publish its ready agent
configuration after provider initialization and catalog composition.
Other tiers SHALL NOT publish a substitute empty catalog. The deployment-profile
and Kafka enable gates SHALL remain, and failures SHALL NOT stop startup.

Inventory SHALL be ordered deterministically before name deduplication and the
250-item limit, including a stable choice when normalized names collide.
Kafka connection parameters and environment-derived event fields SHALL reuse the same resolved settings mapping.

#### Scenario: Only a ready catalog owner announces

- **WHEN** shared bootstrap or a transport, supervisor or background-memory tier starts
- **THEN** it publishes no runtime configuration
- **WHEN** the server or agent-runtime finishes provider initialization and catalog composition
- **THEN** it publishes the ready inventory if the deployment-profile and Kafka gates allow it

#### Scenario: Identical restarts and replicas

- **WHEN** two agent processes start with identical configuration, or one restarts
- **THEN** each start publishes an event, regardless of historical announcement rows

#### Scenario: Reordered inventory preserves the emitted content

- **WHEN** identical inventories are discovered in different orders
- **THEN** their emitted entries are identical, including normalized-name collisions and lists over 250 items

#### Scenario: Event construction reuses the publisher's settings

- **WHEN** the publisher resolves settings for its Kafka parameters
- **THEN** event construction reuses that mapping for environment-derived fields without reloading it
- **WHEN** an explicit mapping is supplied
- **THEN** those parameters and environment-derived fields use the supplied mapping

#### Scenario: Missing or invalid event fields

- **WHEN** a metadata or system-prompt attribute is missing or violates its v1.4 limit or pattern
- **THEN** it is sent as an empty string and a warning names the field and reason without its value
- **AND** the event is still sent when Kafka connection settings are configured
- **AND** this explicitly follows the agreed prose requirement despite conflicting PDF patterns

#### Scenario: Disabled tools

- **WHEN** a catalog tool is disabled through OUROBOROS_DISABLED_TOOLS
- **THEN** its inventory entry remains present with active-tool=false
- **AND** dispatch policy and statically excluded self-modification tools remain unchanged

#### Scenario: MCP inventory is independent of owner selections

- **WHEN** an enabled MCP server has connected but the startup identity has no selected servers
- **THEN** its discovered tools remain in the process startup inventory
- **AND** owner-scoped catalogue reads and dispatch remain restricted by their own selections
- **AND** collection performs no network discovery or settings writes

#### Scenario: Workstation

- **WHEN** WORKSTATION_BASE_URL is configured
- **THEN** aef-workstation is true without a network probe

#### Scenario: Publication fails

- **WHEN** Kafka publication fails
- **THEN** startup continues and the next process start independently attempts publication

### Requirement: A run's trace carries the session of the owner it runs for

The identifier a run stamps on its trace SHALL be read from the durable runtime state of the run's
own owner, through the composed runtime store, rather than from any process-local registry. A
composition that runs tasks without composing a supervisor SHALL stamp the same identifier the
single-process runtime stamps.

An owner with no recorded session SHALL yield an empty identifier and a debug line; a run SHALL NOT
fail because its trace would be unlabelled.

#### Scenario: A tier that runs tasks without a supervisor stamps the session

- **WHEN** the agent-runtime tier executes a task for an owner whose runtime state records a session
- **THEN** the run's spans carry that session identifier

#### Scenario: The session belongs to the owner, not to the process

- **WHEN** one runtime instance executes tasks for two owners with different recorded sessions
- **THEN** each run's spans carry the session of the owner that run belongs to

#### Scenario: A missing session does not fail the run

- **WHEN** a task runs for an owner whose runtime state records no session
- **THEN** the trace carries no session identifier and the run proceeds
