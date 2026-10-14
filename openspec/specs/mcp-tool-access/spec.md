# mcp-tool-access Specification

## Purpose
How MCP tools reach the catalog, how per-owner server selection is enforced, and how dispatch refusal works.
## Requirements
### Requirement: Access is authorization over the declared servers, not per-user connections

The runtime SHALL reach every MCP server through the one process-wide manager, connected from the
list `security_config/mcp.py` exports and authenticated with what the image declares for that server —
nothing, a static token, or the egress gateway's certificate. Whether a run may use a server SHALL be
decided in-process, over the descriptors that manager produces. The runtime SHALL NOT open a connection on
one user's behalf, SHALL NOT accept a server address or credential from a user as configuration, and SHALL
NOT build a second manager or a second tool catalogue per user.

A server whose entry declares a user authorization SHALL additionally carry, on each tool call over
that shared connection, the user's credentials as the `mcp-user-authorization` capability specifies: the
headers and cookies recorded under the run's security token — derived from its owner, agent and surface —
for `user_headers`, or the personal token stored for the server, the run's owner and its agent for
`user_token`. Which credentials a call may carry, under which header names and to which server, SHALL be
decided by the image's declaration and never by the user: a user's request supplies values only for names
the image declared.

`McpServerEntry.enabled` SHALL remain the only switch deciding whether a server is connected: a
server no user may reach SHALL still be connected and initialized.

#### Scenario: A server no user may reach is still connected

- **WHEN** the process starts with an enabled server whose id is outside the user-reachable set
- **THEN** the manager connects it and discovers its tools
- **AND** exactly one manager exists in the process

#### Scenario: No user-supplied server reaches the runtime

- **WHEN** any run asks for a tool from an MCP server
- **THEN** the server it reaches is one the image declared
- **AND** the credentials on the wire are the ones the image declared for that server, plus the user's
  under exactly the header and cookie names that server's entry declares

#### Scenario: A user authorization does not open a connection per user

- **WHEN** runs of two owners call tools of a server declaring a user authorization
- **THEN** both calls travel over the one connection the manager holds for that server
- **AND** no connection, session or tool discovery is started for either owner

### Requirement: The deployment declares which servers users may reach

The per-owner MCP server selection (`OUROBOROS_USER_ENABLED_MCP_IDS`) is resolved as specified in
`user-settings`. This spec owns the access enforcement: the catalog refuses dispatch for servers not
in the resolved set.

#### Scenario: An empty list denies every server

- **WHEN** an agent stores `[]`
- **THEN** no MCP tool is offerable or dispatchable for that agent

#### Scenario: An unknown id grants nothing

- **WHEN** an agent stores an id absent from `security_config/mcp.py`
- **THEN** that id contributes no MCP tool and opens no connection

### Requirement: The MCP provider projects servers by run scope

`McpToolProvider` SHALL be the only component that decides whether an MCP server is usable for a
run. A composition root SHALL pass the declared servers and the bound owner-settings accessor through
a required callable protocol owned by `McpToolProvider`; `UserSettings` SHALL NOT implement an MCP
interface. `McpToolProvider` SHALL NOT import a settings factory, `user_settings`, or `security_config`.
`ToolCatalog` SHALL forward `OwnerScope` to its providers when listing, resolving, refusing, and
executing a tool.

Every provider operation SHALL resolve the allowed ids for its scope and intersect them with the
declared ids. A read failure SHALL deny every MCP server for that operation without failing the task.
Execution SHALL check again before invoking the manager, so a completed settings change affects the
next dispatch but does not cancel a call that already started.

#### Scenario: The decision has one home

- **WHEN** a run lists, resolves, refuses, or executes an MCP tool
- **THEN** its scope reaches `McpToolProvider`
- **AND** no second copy of the server comparison exists

#### Scenario: A settings selection read fails

- **WHEN** the owner server-id selection raises
- **THEN** the provider offers and executes no MCP tool for that operation
- **AND** the task can continue with non-MCP tools

#### Scenario: A cleared setting is observed without reconnecting

- **WHEN** an owner clears the setting after a prior catalogue read
- **THEN** the next catalogue read or dispatch denies that agent every MCP server
- **AND** the process-wide MCP connections remain initialized

### Requirement: The run is not offered an MCP tool it may not call

The tool surface a run reads through its `ToolContext` SHALL exclude the tools of MCP servers the
run may not use: they SHALL NOT appear in the listing of additional tools, SHALL NOT resolve when
named for enabling, and SHALL NOT be counted in the additional-tools note the first prompt carries.
Tools of every other origin SHALL be unaffected.

#### Scenario: The listing omits an unreachable server's tools

- **WHEN** a run lists the additional tools available to it
- **THEN** the tools of servers outside its access set are absent
- **AND** the tools of servers inside it are present

#### Scenario: Enabling an unreachable tool reports it as not found

- **WHEN** a run asks to enable a tool from a server outside its access set
- **THEN** the call reports the name as not found
- **AND** no enable request is recorded for it

#### Scenario: The additional-tools count matches what can be listed

- **WHEN** the first prompt states how many additional tools are available
- **THEN** the number excludes the tools of servers outside the run's access set

### Requirement: Dispatch refuses an MCP tool outside the run's access set

Dispatch SHALL refuse a tool call whose MCP server the provider excludes before the manager runs,
using the provider's normal `refuse_dispatch` path after resolution fails. The refusal SHALL NOT
depend on the offer surface having hidden the name: a name the model produces without being offered
SHALL be refused just as one it was offered.

Enforcement SHALL be fail-closed for a scope: an excluded MCP tool SHALL not resolve or execute.

#### Scenario: A name the model produced unprompted is refused

- **WHEN** a run calls an MCP tool of a server outside its access set without having enabled it
- **THEN** the call is refused before the manager is invoked

#### Scenario: An allowed server's tool dispatches unchanged

- **WHEN** a run calls an MCP tool of a server inside its access set
- **THEN** the call reaches the provider as before

#### Scenario: A non-MCP tool is unaffected

- **WHEN** a run calls a builtin, skill, extension or remote-agent tool
- **THEN** this decision does not refuse it
