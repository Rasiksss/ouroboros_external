# security-config Specification

## Purpose

File-based configuration of the external servers this instance talks to. The MCP server and remote
A2A agent lists live in `security_config/` Python modules that ship with the image and are imported
once at process start, so changing a list is a code change with review, CI and signing behind it
rather than a runtime setting an admin can flip. Each module branches on the free-form
`OUROBOROS_POLIGON` string, builds its entries in code with secrets read from the environment, and
yields an empty list for a polygon it does not configure — which skips manager initialization
instead of failing the start. The legacy settings keys stay in `AppSettings` for the `DEV` branch to
delegate to, and may be read nowhere else. The deployment prompt — the standing instructions a
team adds to the system prompt — is owned the same way, as the single file
`security_config/PROMPT.md`.

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
Each `security_config/` module that exports a list of **reachable outside parties** SHALL read
`load_settings().OUROBOROS_POLIGON` at import time and select its branch by that string; the polygon
and the resulting list SHALL be fixed for the process lifetime. A polygon with no configured branch
SHALL produce an empty list.

A module whose list *restricts* the runtime rather than granting it reach — `confirmations.py`, which
names what the runtime stops to ask about — SHALL NOT branch on the polygon: there, an empty branch
would read as "ask about nothing", which is the open door rather than the closed one.

#### Scenario: A team polygon selects its branch

- **WHEN** `OUROBOROS_POLIGON` is set to a team-specific string that has a branch in the config module
- **THEN** that branch's entries are exported

#### Scenario: An unconfigured polygon yields an empty list

- **WHEN** `OUROBOROS_POLIGON` has no matching branch in the config module
- **THEN** the exported list is empty

### Requirement: Empty list skips manager initialization

Every process that offers the tools of an exported list SHALL skip `mcp_manager.initialize()` /
`a2a_manager.initialize()` when that list is empty, logging an informational message. An empty list
SHALL NOT fail the process start and SHALL NOT initialize a manager with no connections.

The rule belongs to the process that offers the tools rather than to the server alone: the
single-process server and the agent tier of the multi-user deployment both build a tool catalogue
from these lists, and a rule written for one of them leaves the other free to differ.

#### Scenario: No servers configured for the polygon

- **WHEN** the exported MCP list is empty and `MCP_ENABLED` is true
- **THEN** the MCP manager is not initialized
- **AND** the process logs an informational message instead

#### Scenario: No remote agents configured for the polygon

- **WHEN** the exported A2A agent list is empty and `A2A_EGRESS_ENABLED` is true
- **THEN** the A2A egress manager is not initialized
- **AND** the process logs an informational message instead

#### Scenario: The rule holds in every process that offers the tools

- **WHEN** the agent tier of the multi-user deployment starts with an empty exported list
- **THEN** it initializes no manager and logs the same informational message the server logs

### Requirement: Template ships a working example

The shipped `security_config/` templates SHALL contain a working example of constructing
`McpServerEntry` / `A2aRemoteAgentEntry` with a secret read from the environment via a `_token(...)`
helper. The example SHALL live in its own named `EXAMPLE` polygon branch, so that a team copying it
into a branch of its own has a filled-in branch to copy, and so that no polygon reaches it by
accident.

The MCP template's `EXAMPLE` branch SHALL show each credentialed authorization kind once, each entry
declaring exactly one:

- `example-mcp` — an `https://` URL and `auth` of kind `user_token` with `auth_header` `X-API-Key` and a
  static `auth_token` read through `_token("MCP_EXAMPLE_TOKEN")`;
- `example-user-mcp` — an `https://` URL and `auth` of kind `user_headers` forwarding header
  `X-Sudir-Token` and cookie `sudir_session`, carrying no token of its own;
- `example-mtls-mcp` — an `http://` URL of the egress gateway and `auth` of kind `mtls`.

The A2A template's `EXAMPLE` branch SHALL declare its authorization the same way, as exactly one `auth`
and no top-level `auth_header` or `auth_token`:

- `example-agent` — an `https://` URL and `auth` of kind `user_token` with `auth_header` `Authorization`
  and a static `auth_token` read through `_token("A2A_EXAMPLE_TOKEN")`.

#### Scenario: The example polygon exports the example server

- **WHEN** `OUROBOROS_POLIGON` is `EXAMPLE` and the config module is unchanged
- **THEN** the exported list contains the example server whose static token comes from
  `_token("MCP_EXAMPLE_TOKEN")`
- **AND** no other polygon exports it

#### Scenario: The example polygon exports one entry per credentialed kind

- **WHEN** `OUROBOROS_POLIGON` is `EXAMPLE`, the config module is unchanged and `MCP_EXAMPLE_TOKEN` is unset
- **THEN** the exported ids are `example-mcp`, `example-user-mcp` and `example-mtls-mcp`, of kinds
  `user_token`, `user_headers` and `mtls`
- **AND** only `example-mtls-mcp` has an `http://` URL
- **AND** no entry carries a static token of its own
- **AND** no other polygon exports them

#### Scenario: The example polygon exports the example agent with its authorization

- **WHEN** `OUROBOROS_POLIGON` is `EXAMPLE`, `security_config/a2a.py` is unchanged and `A2A_EXAMPLE_TOKEN` is
  `a2a-secret`
- **THEN** the exported A2A list holds exactly `example-agent`, with an `https://` URL
- **AND** its `auth` is of kind `user_token` with `auth_header` `Authorization` and static `auth_token`
  `a2a-secret`
- **AND** no other polygon exports it

#### Scenario: The A2A example carries no secret of its own

- **WHEN** `OUROBOROS_POLIGON` is `EXAMPLE`, `security_config/a2a.py` is unchanged and `A2A_EXAMPLE_TOKEN` is
  unset
- **THEN** `example-agent` declares `auth` of kind `user_token` with no static `auth_token`

### Requirement: The header collector declaration ships in the image

`security_config/auth/header_collector.yaml` SHALL declare which request headers and cookies the browser
transport collects, and `security_config/auth/header_collector.py` SHALL parse it when imported, so the
declaration is changed only by a code change reviewed with the MCP list and the A2A list, and a refused file
stops the process. The shipped declaration SHALL name every header and cookie that the `user_headers`
entries of the `EXAMPLE` polygon's MCP list and A2A list forward, each with a positive `ttl_sec`. A team
that replaces `mcp.py` or `a2a.py` SHALL replace the declaration with them.

The declaration SHALL bind both lists alike. The single-process server, and under the multi-user mode
every tier through the mode's refusal set, SHALL refuse to start when an enabled `user_headers` entry of
either list forwards a header or a cookie the declaration does not declare with that type: an entry of
`security_config/mcp.py` while `MCP_ENABLED` is on, and an entry of `security_config/a2a.py` while
`A2A_EGRESS_ENABLED` is on. The refusal SHALL name the list the entry belongs to and every undeclared name as
the entry id followed by `type:name`, a header name lowercased, and SHALL render no URL or token. A disabled
entry, and an entry of a list whose kill switch is off, SHALL NOT be examined.

#### Scenario: The shipped declaration covers the example

- **WHEN** the shipped declaration is parsed and compared with the names the `EXAMPLE` polygon's MCP list and
  A2A list forward
- **THEN** every forwarded name is declared with its type
- **AND** every declared parameter has a positive `ttl_sec`

#### Scenario: An undeclared name forwarded by an A2A agent stops the process

- **WHEN** `A2A_EGRESS_ENABLED` is on, the declaration names header `X-Sudir-Token` and cookie
  `sudir_session`, and the enabled A2A list holds `research` declaring `user_headers` with header `X-Other`
- **THEN** the single-process server refuses to start, before the A2A client connects, naming
  `security_config/a2a.py` and `research header:x-other`
- **AND** under the multi-user mode the mode's refusal set names `security_config/a2a.py` and
  `research header:x-other`

#### Scenario: An undeclared name forwarded by an MCP server stops the process

- **WHEN** `MCP_ENABLED` is on, the declaration names header `X-Sudir-Token` and cookie `sudir_session`, and
  the enabled MCP list holds `hr` declaring `user_headers` with header `X-Other`
- **THEN** the single-process server refuses to start naming `security_config/mcp.py` and `hr header:x-other`
- **AND** under the multi-user mode the mode's refusal set names `security_config/mcp.py` and
  `hr header:x-other`

#### Scenario: A list behind an off kill switch is not examined

- **WHEN** `A2A_EGRESS_ENABLED` is off and the A2A list holds an enabled `user_headers` entry forwarding an
  undeclared header, or the entry is disabled while the kill switch is on
- **THEN** neither the server nor the mode's refusal set refuses for that entry

#### Scenario: An empty declaration file is refused

- **WHEN** the declaration file is empty
- **THEN** parsing it raises

### Requirement: Legacy keys are read only inside security_config

Product code outside `security_config/` SHALL NOT reference `MCP_SERVERS` or `A2A_REMOTE_AGENTS`,
except `ouroboros/storage/settings/schema.py` where the fields are defined, validated, and
serialized. A static scan of every product root that can compose a runtime — `ouroboros/`,
`supervisor/`, `new_core/`, `background_memory/`, `transport/`, `multiuser_config/`, and `server.py`
— SHALL find no other references.

A composition root left out of the scan is a root where the confinement holds by nobody having
written the read yet. The multi-user tiers are composition roots and are scanned as such.

#### Scenario: The static scan allows only the sanctioned readers

- **WHEN** scanning Python files in `ouroboros/`, `supervisor/`, `new_core/`, `background_memory/`, `transport/`, `multiuser_config/`, and `server.py` for `MCP_SERVERS` or `A2A_REMOTE_AGENTS`
- **THEN** every reference is in `ouroboros/storage/settings/schema.py` or under `security_config/`

#### Scenario: A tier reading the legacy key fails the scan

- **WHEN** a composition root under `multiuser_config/` reads `MCP_SERVERS` from settings
- **THEN** the scan reports it as a violation

#### Scenario: A settings getter for the legacy lists is removed

- **WHEN** `get_mcp_servers()` or `get_a2a_remote_agents()` is called
- **THEN** the call fails at import time, because the functions no longer exist in `ouroboros/app/config.py`

### Requirement: The deployment prompt is sourced from security_config/PROMPT.md

The system SHALL read `security_config/PROMPT.md` at every system prompt build, the way it reads
`prompts/SYSTEM.md`, and SHALL place its text directly after the base prompt in the base cache
block, under one `## Deployment instructions` section whose header states that the instructions
extend and narrow the rules above and do not override them. An empty or missing file SHALL add no
section. The section SHALL declare no drop priority and a budget of 27 000 characters of section
text, header included; a section over that budget SHALL be logged as a warning naming the section
and SHALL reach the prompt intact.
The file SHALL NOT branch on `OUROBOROS_POLIGON`: one deployment ships one file.

#### Scenario: The file follows the base prompt in its cached block

- **WHEN** `security_config/PROMPT.md` holds text
- **THEN** the first content block of the system prompt is the base prompt followed by
  `## Deployment instructions`, the header, and the file's text
- **AND** that block carries the base prompt's cache control and no further breakpoint is added

#### Scenario: An empty or missing file adds no section

- **WHEN** `security_config/PROMPT.md` is empty, whitespace only, or absent
- **THEN** the system prompt has no `## Deployment instructions` heading

#### Scenario: Over budget is a warning, not a cut

- **WHEN** the rendered section exceeds 27 000 characters
- **THEN** a warning names `deployment_prompt` and the sizes
- **AND** the text is in the system prompt in full
