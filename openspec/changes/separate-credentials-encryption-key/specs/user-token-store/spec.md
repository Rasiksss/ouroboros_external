## REMOVED Requirements

### Requirement: The SQL backend stores one sealed token per target, owner and agent

**Reason**: Tokens are sealed with AES-256-SIV under `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` instead of
AES-256-GCM under `OUROBOROS_CHAT_ENCRYPTION_KEY`, so no row sealed before this change opens, and the
scenario that kept `mcp_user_tokens` rows sealed before `0019` opening no longer holds. What `0019` leaves of
`mcp_user_tokens` stays pinned by "Migration 0019 adds its table and nothing else".
**Migration**: No schema migration. Set `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` in every tier, delete the rows
of `mcp_user_tokens` and `a2a_user_tokens`, and owners set their personal tokens again. The behavior is
restated by "The SQL backend seals one token per target, owner and agent under the credentials key".

## ADDED Requirements

### Requirement: The SQL backend seals one token per target, owner and agent under the credentials key

The system SHALL provide a SQL backend of `IUserTokenStore` over one runtime database table per target
kind, chosen by the target's kind and never by a prefix or shape of its id:

- `mcp_user_tokens`, for `MCP_SERVER` targets, created by migration `0018` with the columns `id`,
  `mcp_server_id`, `agent_id`, `owner_id`, `token` and `expires_at`, all text and not null, the primary key
  `pk_mcp_user_tokens` on `id`, and the unique constraint `uq_mcp_user_tokens_server_owner_agent` on
  `(mcp_server_id, owner_id, agent_id)`;
- `a2a_user_tokens`, for `A2A_AGENT` targets, created by the additive migration `0019`, whose down revision
  is `0018`, with the columns `id`, `a2a_agent_id`, `agent_id`, `owner_id`, `token` and `expires_at`, all
  text and not null, the primary key `pk_a2a_user_tokens` on `id`, the unique constraint
  `uq_a2a_user_tokens_remote_owner_agent` on `(a2a_agent_id, owner_id, agent_id)` and the index
  `idx_a2a_user_tokens_owner_agent` on `(owner_id, agent_id)`,

so that a second row for one target, owner and agent is refused. Each migration SHALL refuse to run when
its table already exists, and its downgrade SHALL drop its table only when it is present. Migration `0019`
SHALL NOT alter `mcp_user_tokens` or any of its rows, and its downgrade SHALL drop `a2a_user_tokens` alone.

The backend SHALL be constructed with the async engine and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, neither
with a default, and SHALL refuse construction when the key, with whitespace removed, is not exactly 64 bytes
of valid base64, naming the setting and not rendering the key.

`token` SHALL be sealed as the security context store seals a value (`security-context`): AES-256-SIV under
that key with a fresh random nonce, on every database dialect and independently of message field
encryption, with a JSON array as associated data that differs by kind: `[mcp_server_id, owner_id, agent_id]`
for an MCP server and `["a2a-agent", a2a_agent_id, owner_id, agent_id]` for an A2A agent, so that a
ciphertext moved to another target, owner or agent, or between the two tables, does not decrypt. A token
sealed under `OUROBOROS_CHAT_ENCRYPTION_KEY`, as tokens were before the credentials key existed, SHALL NOT
open. `expires_at` SHALL be fixed-width ISO-8601 UTC text in the format of `security_contexts`, compared as
text. A load SHALL select only the row of the requested target, owner and agent, in the table of the
target's kind, whose `expires_at` is after now, SHALL decrypt only it, and SHALL raise, never return nothing,
when that row does not decrypt.

The writer `SqlUserTokenRepo` SHALL seal with exactly that associated data and expiry format; `save` SHALL
be one upsert on the unique constraint of the target's table, `clear` SHALL delete only the row of that
target, owner and agent in that table, and `expirations(kind, owner_id, agent_id)` SHALL read only the table
of that kind and open no token. A load reaches one row through its table's unique constraint; `expirations`
names no target id, and on `a2a_user_tokens` it reaches the owner's rows through
`idx_a2a_user_tokens_owner_agent`. `mcp_user_tokens` carries no such index and its listing scans, which is
`0018`'s to change and not `0019`'s.

#### Scenario: A stored token loads

- **WHEN** a sealed token for target `t` — MCP server `s` or A2A agent `r` — owner `o` and agent `a`
  expiring in the future is stored, and the store is asked for `t`, `o` and `a`
- **THEN** it returns the token

#### Scenario: Another server, owner or agent loads nothing

- **WHEN** a token for MCP server `s`, owner `o` and agent `a` is stored and the store is asked with a
  different server id, a different owner or a different agent
- **THEN** it returns nothing

#### Scenario: Another A2A agent, owner or agent loads nothing

- **WHEN** a token for A2A agent `r`, owner `o` and agent `a` is stored and the store is asked with a
  different remote agent id, a different owner or a different agent
- **THEN** it returns nothing

#### Scenario: An MCP server and an A2A agent with one id hold separate tokens

- **WHEN** token `x` is stored for MCP server `hr` and token `y` for A2A agent `hr`, both for owner `o` and
  agent `a`
- **THEN** the store returns `x` for the MCP server target and `y` for the A2A agent target
- **AND** after the A2A agent target is cleared the store still returns `x` for the MCP server target and
  nothing for the A2A agent target

#### Scenario: An expired token loads nothing

- **WHEN** the stored token's `expires_at` has passed
- **THEN** the store returns nothing

#### Scenario: A second row for one server, owner and agent is refused

- **WHEN** a second row with the same `mcp_server_id`, owner and agent is inserted into `mcp_user_tokens`
- **THEN** the insert violates `uq_mcp_user_tokens_server_owner_agent`

#### Scenario: A second row for one A2A agent, owner and agent is refused

- **WHEN** a second row with the same `a2a_agent_id`, owner and agent is inserted into `a2a_user_tokens`
- **THEN** the insert violates `uq_a2a_user_tokens_remote_owner_agent`

#### Scenario: The table holds no token in the clear

- **WHEN** a token is stored for a target of either kind
- **THEN** no column of its row contains the token

#### Scenario: A moved or foreign ciphertext raises

- **WHEN** the stored ciphertext of one target, owner and agent is copied onto the row of another, or from
  the `mcp_user_tokens` row of `hr` onto the `a2a_user_tokens` row of `hr` with the same owner and agent or
  back, or the row was sealed under another key, and that row is loaded
- **THEN** the load raises

#### Scenario: A token sealed under the chat encryption key does not open

- **WHEN** an `mcp_user_tokens` row holds a token sealed with AES-256-GCM under a chat encryption key with
  the associated data `[mcp_server_id, owner_id, agent_id]`, as before the credentials key existed, and the
  store is asked for that MCP server, owner and agent
- **THEN** the load raises

#### Scenario: Expirations are scoped to one kind

- **WHEN** owner `o` and agent `a` hold an unexpired token for MCP server `hr` and one for A2A agent
  `research`
- **THEN** `expirations(MCP_SERVER, o, a)` names `hr` and not `research`
- **AND** `expirations(A2A_AGENT, o, a)` names `research` and not `hr`

#### Scenario: Migration 0019 adds its table and nothing else

- **WHEN** a database at `0018` is upgraded to `0019`
- **THEN** `a2a_user_tokens` exists with `pk_a2a_user_tokens` on `id`,
  `uq_a2a_user_tokens_remote_owner_agent` on `(a2a_agent_id, owner_id, agent_id)` and
  `idx_a2a_user_tokens_owner_agent` on `(owner_id, agent_id)`
- **AND** the columns and rows of `mcp_user_tokens` are unchanged, and it gains no index

#### Scenario: Migration 0019 refuses a partly run revision

- **WHEN** a database at `0018` that already holds a table `a2a_user_tokens` is upgraded to `0019`
- **THEN** the migration raises naming `a2a_user_tokens`
- **AND** no table is created or changed

#### Scenario: The downgrade of 0019 drops only its table

- **WHEN** a database at `0019` holding rows in both tables is downgraded to `0018`
- **THEN** `a2a_user_tokens` no longer exists
- **AND** `mcp_user_tokens` and its rows remain

#### Scenario: An unusable key is refused at construction

- **WHEN** the backend is constructed with an empty, non-base64, 16-, 32- or 48-byte key
- **THEN** construction raises naming `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` and not the value

## MODIFIED Requirements

### Requirement: Every root that composes MCP or A2A egress installs the user token store its lists need

Every composition root that connects MCP servers or remote A2A agents — the single-process server and the
agent tier of the multi-user mode — SHALL build one `EgressAuthDependencies` holding its security context
store and its user token store, SHALL hand that value to `McpToolProvider` and to the MCP manager's
initialization, and SHALL hand one `A2aEgressDependencies` whose `auth` is that same value to
`A2aToolProvider` and to the A2A manager's initialization, never per call. No root SHALL build a second
user token store for either client.

The user token store SHALL be composed by `EgressAuthComposition` (`ouroboros/app/egress_auth_composition.py`)
from both lists the root connects — the MCP list behind `MCP_ENABLED` and the A2A list behind
`A2A_EGRESS_ENABLED` — and SHALL be the SQL backend over the root's async engine whatever those lists hold;
no production root installs the discarding backend. When the SQL backend refuses the
`OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` it is composed with, the composition SHALL raise
`EgressAuthCompositionError` naming every enabled entry of either list that takes a personal token — an MCP
`user_token` server over `streamable_http` and an A2A `user_token` agent — (or `none`) and the setting and
not rendering the key. A root never reaches it without a valid key, because its security context store,
composed earlier from the same key, refuses first. An MCP `user_token` entry over `sse` SHALL NOT require the
store and SHALL NOT be named.

The agent tier SHALL initialize the async engine beside its store before building the dependencies and
SHALL dispose it on its tool-client loop when it shuts down, within the exit budget it shares with the MCP
and A2A managers.

#### Scenario: A list without a streamable user_token server still installs the SQL store

- **WHEN** a root composes an MCP list whose enabled entries declare `none`, `user_headers`, `mtls`, or
  `user_token` over `sse` only, and an A2A list whose enabled entries declare `none`, `user_headers` or
  `mtls` only, with a valid key
- **THEN** it installs the SQL user token store over its async engine

#### Scenario: A streamable user_token server installs the SQL store

- **WHEN** a root composes a list holding an enabled `user_token` entry `hr` over `streamable_http`, with a
  valid key
- **THEN** it installs the SQL user token store over its async engine

#### Scenario: A user_token A2A agent installs the SQL store

- **WHEN** a root composes an A2A list holding an enabled `user_token` agent `research`, with a valid key
- **THEN** it installs the SQL user token store over its async engine

#### Scenario: A streamable user_token server without a valid key refuses the composition

- **WHEN** the user token store is composed for an MCP list holding enabled `user_token` entry `hr` over
  `streamable_http`, even with a static token, and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` is empty
- **THEN** the composition raises naming `hr` and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`
- **AND** the error does not contain the key

#### Scenario: Personal-token entries of both lists without a valid key are all named

- **WHEN** the user token store is composed for an MCP list holding enabled `user_token` entry `hr` over
  `streamable_http`, even with a static token, and enabled `user_token` entry `legacy` over `sse`, and an A2A
  list holding enabled `user_token` agent `research`, and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` is empty
- **THEN** the composition raises naming `hr`, `research` and `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`
- **AND** the error names neither `legacy` nor the key

#### Scenario: The dependencies are built once

- **WHEN** the single-process server or the agent tier composes MCP and A2A egress
- **THEN** `McpToolProvider` and the MCP manager's initialization receive the same `EgressAuthDependencies`
  value
- **AND** `A2aToolProvider` and the A2A manager's initialization receive the same `A2aEgressDependencies`,
  whose `auth` is that value
