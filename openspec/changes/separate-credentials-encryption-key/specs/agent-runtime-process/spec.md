## MODIFIED Requirements

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
egress dependencies, beside task handles keyed by the tier's `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, and
the A2A manager SHALL be initialized with those dependencies, so every A2A connection the tier opens
authorizes its calls from the stores every tier shares and issues handles every replica opens. A second
authorization value per client would be a second place for the stores and the key to diverge.

The stores and the task handles SHALL be composed from `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` alone;
`OUROBOROS_CHAT_ENCRYPTION_KEY` SHALL play no part in them. A key the stores or the task handles refuse
SHALL fail the composition before any connection is opened, naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`
and rendering no part of its value. The tier SHALL add no refusal of its own for the A2A list: an
instance-local address and an undeclared forwarded name are the mode's refusals, made by the shared
start-up before this composition.

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

- **WHEN** the agent tier composes with A2A egress on, a valid `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and an
  empty `OUROBOROS_CHAT_ENCRYPTION_KEY`
- **THEN** a handle sealed by task handles built over that same key, for an owner, an agent and a remote
  agent, opens under the task handles the A2A manager received, for that owner, agent and remote agent

#### Scenario: A key the tier cannot seal with opens no A2A connection

- **WHEN** the agent tier composes with A2A egress on, a non-empty remote-agent list, a valid
  `OUROBOROS_CHAT_ENCRYPTION_KEY`, and an empty, malformed or 32-byte `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`
- **THEN** composition fails naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and rendering no part of the value
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
