# agent-runtime-process Specification

## Purpose
The agent runtime as its own process — composition, kill switches, manager lifecycle, and probes.
## Requirements
### Requirement: The agent tier owns a client event loop for its external tool sources

The agent tier's composition root SHALL own an asyncio event loop for the process lifetime, running
in a thread of its own, and SHALL hand it to every tool source whose client is a coroutine client.

The single-process server inherits this loop from uvicorn; a tier started as `python -m` has no
server and therefore no loop, and an MCP session cannot exist without one. The loop SHALL be started
before the tier composes its tool catalogue and SHALL be stopped after the tier's runs have drained,
so that no client is reached on a loop that is already closed and no session outlives the process
that opened it.

The loop SHALL belong to the agent tier's own root rather than to the start-up shared by every tier:
the tiers that run no tools need no loop, and the shared start-up owns nothing component-specific.

#### Scenario: The tier composes with a loop of its own

- **WHEN** the agent tier process composes itself
- **THEN** an event loop is running in a thread owned by that process before the tool catalogue is built

#### Scenario: The loop is stopped after the runs drain

- **WHEN** the agent tier process is stopping
- **THEN** the runs drain first, then the tool sources are shut down, then the loop is stopped and its thread joined

#### Scenario: The loop is stopped even when the runtime fails

- **WHEN** the tier's runtime raises instead of returning
- **THEN** the shutdown and the loop stop still run, and the process does not leave the loop thread behind

#### Scenario: The shared start-up does not own the loop

- **WHEN** a tier that runs no tasks starts
- **THEN** no client event loop is started for it

### Requirement: The agent tier initializes its tool sources while it builds the catalogue

The agent tier SHALL initialize every tool source whose descriptors are read from a live client —
the MCP and outbound A2A managers among them — during the catalogue's initialization, and SHALL do so through
the source's own provider construction rather than through an initialization path of its own.

Under the multi-user mode the catalogue is refreshed exactly once, when it is composed: the task path
refreshes nothing, so a provider that receives its client after that point is a provider whose
connect never happens. Doing it through the provider's constructor keeps one initialization path in
the tree; a second one would be a second place for the connect, the timeout and the failure log to
drift.

A source that fails to connect SHALL be logged and skipped, and SHALL NOT fail the tier's start —
the tier serves tasks that do not use it.
The A2A initialization wait SHALL follow card discovery's own connection bound rather than the
shorter task-operation timeout, so the multi-user and single-process roots accept the same peer.

Before it constructs either provider, the tier SHALL compose one egress authorization value — the SQL
security context store it installs for the process and one user token store composed over the MCP list
behind `MCP_ENABLED` together with the A2A list behind `A2A_EGRESS_ENABLED` — and SHALL hand that same
value to the MCP provider and to the A2A provider. The A2A provider SHALL receive it inside the A2A
egress dependencies, beside task handles keyed by the tier's `OUROBOROS_CHAT_ENCRYPTION_KEY`, and the
A2A manager SHALL be initialized with those dependencies, so every A2A connection the tier opens
authorizes its calls from the stores every tier shares and issues handles every replica opens. A second
authorization value per client would be a second place for the stores and the key to diverge.

A key the stores or the task handles refuse SHALL fail the composition before any connection is opened,
naming `OUROBOROS_CHAT_ENCRYPTION_KEY` and rendering no part of its value. The tier SHALL add no refusal
of its own for the A2A list: an instance-local address and an undeclared forwarded name are the mode's
refusals, made by the shared start-up before this composition.

#### Scenario: MCP descriptors are in the catalogue the tier builds

- **WHEN** the agent tier starts with the kill switch on and a non-empty server list
- **THEN** the MCP manager is initialized on the tier's loop
- **AND** the catalogue the tier builds contains that server's tools

#### Scenario: A2A descriptors are in the catalogue the tier builds

- **WHEN** the agent tier starts with A2A egress on and a non-empty remote-agent list
- **THEN** the A2A manager is initialized on the tier's loop
- **AND** it is initialized with the A2A egress dependencies the tier composed
- **AND** the catalogue contains `a2a__<agent>`, `a2a_task_status`, and `a2a_task_cancel`

#### Scenario: Both clients authorize with one value

- **WHEN** the agent tier composes with the MCP kill switch and A2A egress on
- **THEN** the authorization dependencies the MCP provider holds and those inside the A2A manager's
  egress dependencies are one value
- **AND** its security context store is the SQL store the tier installed, and its user token store is
  the SQL store over the runtime database

#### Scenario: The A2A manager's task handles are keyed by the tier's encryption key

- **WHEN** the agent tier composes with A2A egress on and a valid `OUROBOROS_CHAT_ENCRYPTION_KEY`
- **THEN** a handle sealed by task handles built over that same key, for an owner, an agent and a remote
  agent, opens under the task handles the A2A manager received, for that owner, agent and remote agent

#### Scenario: A key the tier cannot seal with opens no A2A connection

- **WHEN** the agent tier composes with A2A egress on, a non-empty remote-agent list, and an empty or
  malformed `OUROBOROS_CHAT_ENCRYPTION_KEY`
- **THEN** composition fails naming `OUROBOROS_CHAT_ENCRYPTION_KEY` and rendering no part of the value
- **AND** no A2A connection is opened

#### Scenario: Connecting happens at composition, not on the task path

- **WHEN** the tier has composed and a task then begins
- **THEN** the connect has already happened, once
- **AND** the task path neither reconnects nor discovers a connection that was never made

#### Scenario: A source that cannot connect does not stop the tier

- **WHEN** a configured MCP server refuses the connection at tier start
- **THEN** the failure is logged and the tier starts, with that server's tools absent from the catalogue

#### Scenario: One unreachable A2A agent does not hide its peers

- **WHEN** one configured remote agent cannot be initialized and another can
- **THEN** the tier starts and the reachable agent remains in the catalogue

#### Scenario: A low tool timeout does not shorten card discovery

- **WHEN** `A2A_TOOL_TIMEOUT_SEC` is below the card discovery timeout and a card answers within its
  own bound
- **THEN** the tier completes initialization and offers that agent's tool

### Requirement: The kill switch decides what the agent tier connects to, not only what it dispatches

When the MCP or A2A egress kill switch is off, the agent tier SHALL connect to nothing for that
source: no session is opened and no remote endpoint is contacted.

The MCP manager may additionally check live availability at dispatch, while the A2A manager binds
its enabled state at startup. In both cases the composition root SHALL apply the switch before
initialization: a tier that connects first and only then discovers the switch is off has already
opened the sessions the switch exists to prevent.

#### Scenario: The switch off means no connection

- **WHEN** the agent tier starts with the kill switch off and a non-empty server list
- **THEN** no MCP session is opened
- **AND** the catalogue contains no MCP tools

#### Scenario: The switch on with an empty list connects to nothing

- **WHEN** the agent tier starts with the kill switch on and an empty server list
- **THEN** no manager is initialized and the tier logs that it had nothing to connect to

#### Scenario: A2A egress off means no connection

- **WHEN** the agent tier starts with `A2A_EGRESS_ENABLED` off and a non-empty remote-agent list
- **THEN** no A2A connection is opened and the catalogue contains no A2A tools

### Requirement: Every tool manager is closed before the client loop stops

After runs drain, the agent tier SHALL close the MCP and A2A managers on their shared client loop.
Both closes SHALL start independently and share one bounded shutdown budget, so a failure or hang in
one manager cannot skip the other or extend the process past two sequential budgets.

#### Scenario: One manager fails during shutdown

- **WHEN** either MCP or A2A raises while closing
- **THEN** the other manager is still closed and the client loop is still stopped

#### Scenario: One manager hangs during shutdown

- **WHEN** either manager does not close within the shared shutdown budget
- **THEN** its close is cancelled and the process continues stopping the client loop

### Requirement: The agent tier serves the health surface beside its loop

The agent tier SHALL serve the startup, readiness and liveness probes on the port it is configured
with, from a thread of its own once the runtime is composed, so that an orchestrator has a probe per
container rather than per tier that happens to serve HTTP. A tier that could not serve them SHALL
claim no work. The probes SHALL answer about this process only: startup succeeds once the runtime is
composed, readiness succeeds once it is composed and the runtime database answers the connection this
process holds for the probe, and liveness reports the claim loop's heartbeat against the lease
heartbeat's own staleness bound. A task that wedged keeps the claim loop iterating and is the
supervisor's timeout sweep's to end, not this probe's.

A tier whose database is unreachable claims nothing it could finish, so it SHALL say so on readiness
and SHALL NOT say so on liveness: the claim loop keeps beginning iterations throughout, restarting
the process would abandon the runs it holds, and the tier resumes claiming by itself once the
database answers.

The surface SHALL be stopped after the runs drain and before the process exits, so that no probe
answers for a runtime that no longer claims.

#### Scenario: The probes answer while the loop runs

- **WHEN** the agent tier has composed its runtime
- **THEN** startup, readiness and liveness answer success on the configured port

#### Scenario: A silent claim loop fails liveness

- **WHEN** the claim loop has not begun an iteration within the stall bound
- **THEN** liveness answers failure naming how long the loop has been silent
- **AND** readiness is unaffected

#### Scenario: An unreachable database fails readiness alone

- **WHEN** the runtime database refuses the connection this tier holds for the probe
- **THEN** readiness answers not ready
- **AND** liveness answers success while the claim loop keeps beginning iterations

#### Scenario: The surface leaves with the loop

- **WHEN** the runtime has drained and the process is exiting
- **THEN** the probe server is stopped before the process exits

### Requirement: Tool-provider initialization ownership is explicit

Every A2A tool provider SHALL be constructed in exactly one of two modes: an external async lifespan
owns manager initialization, or the provider owns initialization on a supplied event loop from a
supplied agent list. Both composition roots SHALL select that mode explicitly. A partially supplied
provider-owned mode SHALL fail at construction rather than silently offering no tools.

#### Scenario: The single-process lifespan owns initialization

- **WHEN** the single-process server constructs its A2A provider
- **THEN** it explicitly selects external lifecycle ownership
- **AND** the provider does not initialize the manager during catalogue refresh

#### Scenario: The agent tier owns initialization

- **WHEN** the agent-runtime process constructs its A2A provider
- **THEN** it supplies the enabled agent list and its client event loop
- **AND** the provider initializes the manager during catalogue composition

#### Scenario: Partial provider lifecycle configuration is refused

- **WHEN** a provider is constructed with an agent list but no event loop, or an event loop but no agent list
- **THEN** construction fails before catalogue initialization

### Requirement: Provider initialization is bounded and observable

The agent-runtime process SHALL use the shared synchronous cross-loop bridge to wait for MCP and A2A
manager initialization. A timed-out wait SHALL cancel its initialization coroutine, and successful
A2A initialization SHALL log the number of connected agents, including zero.

#### Scenario: A2A initialization completes

- **WHEN** provider-owned A2A initialization returns within its discovery budget
- **THEN** the process logs the number of connections the manager exposes

#### Scenario: Provider initialization exceeds its budget

- **WHEN** MCP or A2A provider-owned initialization exceeds its bounded wait
- **THEN** its initialization coroutine is cancelled
- **AND** catalogue composition continues without failing the process
