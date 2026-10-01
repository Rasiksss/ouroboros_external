# security-config Specification

## Purpose

File-based configuration of the external servers this instance talks to. The MCP server and remote
A2A agent lists live in `security_config/` Python modules that ship with the image and are imported
once at process start, so changing a list is a code change with review, CI and signing behind it
rather than a runtime setting an admin can flip. Each module branches on the free-form
`OUROBOROS_POLIGON` string, builds its entries in code with secrets read from the environment, and
yields an empty list for a polygon it does not configure — which skips manager initialization
instead of failing the start. The legacy settings keys stay in `AppSettings` for the `DEV` branch to
delegate to, and may be read nowhere else.

## Requirements

### Requirement: Server lists are sourced from security_config modules

The runtime SHALL source MCP server and remote A2A agent lists from `security_config/mcp.py`
(`mcp_servers`) and `security_config/a2a.py` (`a2a_remote_agents`) respectively, imported once at
process start. These modules SHALL be plain Python and SHALL construct `McpServerEntry`/
`A2aRemoteAgentEntry` entries, reading secrets from the environment or a team-owned secret engine
invoked at import time. The lists SHALL be flat lists of entries; a module SHALL NOT produce a
nested list. The legacy settings keys `MCP_SERVERS`/`A2A_REMOTE_AGENTS` SHALL remain in `AppSettings`
unchanged, and reading them SHALL be allowed only inside `security_config/` modules and in
`schema.py` (definition, validation, serialization).

#### Scenario: The process starts with entries from the config module

- **WHEN** the process starts with `security_config/mcp.py` exporting a flat list of `McpServerEntry`
- **THEN** the MCP manager is initialized with exactly those entries
- **AND** no other component reads `MCP_SERVERS`/`A2A_REMOTE_AGENTS` outside `security_config/` and `schema.py`

#### Scenario: The DEV polygon delegates to legacy settings

- **WHEN** `OUROBOROS_POLIGON` is `DEV` and the DEV branch assigns `list(load_settings().MCP_SERVERS)`
- **THEN** `mcp_servers` equals the configured legacy list
- **AND** the result is a flat list, never a list containing a list

#### Scenario: Secrets come from the environment

- **WHEN** a config module builds an entry with `_token("MCP_EXAMPLE_TOKEN")`
- **THEN** the entry's auth token is the value of the `MCP_EXAMPLE_TOKEN` environment variable
- **AND** no secret literal is stored in the module source

### Requirement: Polygon selects the configuration branch

`OUROBOROS_POLIGON` SHALL be a free-form string without validation or allowed-value constraints.
Each `security_config/` module SHALL read `load_settings().OUROBOROS_POLIGON` at import time and
select its branch by that string; the polygon and the resulting list SHALL be fixed for the process
lifetime. A polygon with no configured branch SHALL produce an empty list.

#### Scenario: A team polygon selects its branch

- **WHEN** `OUROBOROS_POLIGON` is set to a team-specific string that has a branch in the config module
- **THEN** that branch's entries are exported

#### Scenario: An unconfigured polygon yields an empty list

- **WHEN** `OUROBOROS_POLIGON` has no matching branch in the config module
- **THEN** the exported list is empty

### Requirement: Empty list skips manager initialization

The server SHALL skip `mcp_manager.initialize()` / `a2a_manager.initialize()` when the exported list
is empty, logging an informational message. An empty list SHALL NOT fail the process start and SHALL
NOT initialize a manager with no connections.

#### Scenario: No servers configured for the polygon

- **WHEN** the exported MCP list is empty and `MCP_ENABLED` is true
- **THEN** the MCP manager is not initialized
- **AND** the process logs an informational message instead

#### Scenario: No remote agents configured for the polygon

- **WHEN** the exported A2A agent list is empty and `A2A_EGRESS_ENABLED` is true
- **THEN** the A2A egress manager is not initialized
- **AND** the process logs an informational message instead

### Requirement: Template ships a working example

The shipped `security_config/` templates SHALL contain a working example of constructing
`McpServerEntry` / `A2aRemoteAgentEntry` with a secret read from the environment via a `_token(...)`
helper. The example SHALL live in its own named `EXAMPLE` polygon branch, so that a team copying it
into a branch of its own has a filled-in branch to copy, and so that no polygon reaches it by
accident.

#### Scenario: The example polygon exports the example server

- **WHEN** `OUROBOROS_POLIGON` is `EXAMPLE` and the config module is unchanged
- **THEN** the exported list contains the example server whose token comes from `_token("MCP_EXAMPLE_TOKEN")`
- **AND** no other polygon exports it

### Requirement: Legacy keys are read only inside security_config

Product code outside `security_config/` SHALL NOT reference `MCP_SERVERS` or `A2A_REMOTE_AGENTS`,
except `ouroboros/storage/settings/schema.py` where the fields are defined, validated, and
serialized. A static scan of `ouroboros/`, `supervisor/`, `new_core/`, and `server.py` SHALL find no
other references.

#### Scenario: The static scan allows only the sanctioned readers

- **WHEN** scanning Python files in `ouroboros/`, `supervisor/`, `new_core/`, and `server.py` for `MCP_SERVERS` or `A2A_REMOTE_AGENTS`
- **THEN** every reference is in `ouroboros/storage/settings/schema.py` or under `security_config/`

#### Scenario: A settings getter for the legacy lists is removed

- **WHEN** `get_mcp_servers()` or `get_a2a_remote_agents()` is called
- **THEN** the call fails at import time, because the functions no longer exist in `ouroboros/app/config.py`
