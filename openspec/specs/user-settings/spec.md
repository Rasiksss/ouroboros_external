## Purpose

Per-owner overrides of the settings a user is allowed to change. Overrides live in the sparse
runtime-database table `user_settings`, where the absence of a row normally means the instance value
applies. Everything else in `AppSettings` stays instance-only: those keys decide where data goes,
which credentials sign it, and how the deployment behaves as a whole. One `user_settings` object is
assembled when the module is imported and readers import it; nothing builds its own or resolves
settings implicitly. Accessors take one argument, the owner together with the agent, and each
reader passes the scope of the work in hand. Two agents of one owner hold independent values and an
agent with no row of its own resolves the instance value for instance-backed keys. Overrides are
written through the HTTP surface specified by `user-settings-surface`, and through the owner's own
chat command for the one key a chat command reaches.
## Requirements
### Requirement: Every settable key is keyed by the owner together with the agent

The system SHALL declare, in one place read by every reader and every writer, which keys an owner
may change. A key absent from that declaration SHALL be unknown to both the reader and the writer.

Every override SHALL be stored keyed by the owner, the agent and the key, so that two agents of one
owner hold independent values and no query can reach another owner's row. There is no owner-wide
value an agent inherits: an agent with no row of its own resolves the instance value. The store SHALL
remain sparse: the absence of a row means the instance value applies.

#### Scenario: The declaration is the only place the keys are stated

- **WHEN** the reader and the writer decide whether a key exists
- **THEN** both read the same declaration

#### Scenario: Two agents of one owner hold independent values

- **WHEN** an owner stores different `OUROBOROS_MODEL` overrides for two of their agents
- **THEN** each agent resolves its own override
- **AND** a third agent of the same owner resolves the instance value

#### Scenario: A value is not shared across an owner's agents

- **WHEN** an owner stores an `OUROBOROS_TIMEZONE` override for one agent
- **THEN** that agent resolves the overridden timezone
- **AND** another agent of the same owner resolves the instance value

#### Scenario: One owner's override does not reach another owner

- **WHEN** two owners store overrides under the same agent id
- **THEN** each owner resolves their own
- **AND** neither reads the other's

### Requirement: The store is re-keyed in place and its rows carried over

The revision that adds the agent to the key SHALL rebuild the store with the owner, the agent and the
key as its key, and SHALL carry every existing row over under the default agent's reserved id rather than
dropping or stranding it. Its reversal SHALL restore the owner-and-key shape and keep the default agent's
rows, which are the only ones that shape can hold.

#### Scenario: An existing row survives under the default agent

- **WHEN** the revision runs against a store holding an owner's row
- **THEN** the row is present afterwards under that owner and the default agent's id

#### Scenario: The revision is reversible

- **WHEN** the revision is reversed
- **THEN** the store is keyed by the owner and the key again
- **AND** the default agent's rows are kept and every other agent's are gone

### Requirement: A key resolves to the stored override, else the instance value

The system SHALL resolve each key in the declaration to the override stored for the owner and the
agent of the scope passed to the accessor when one exists, and to the instance value otherwise. Rows
for keys outside the declaration SHALL be ignored when the store is read. Overrides are stored as
text; an override that does not parse as the key's type SHALL resolve to the instance value.

Resolution SHALL be unchanged by whether a cache is composed: the same scope resolves the same value
with a cache as without one, except that a write made outside the HTTP surface may take until the
cache's expiry window to be seen (`user-settings-cache`).

#### Scenario: No override stored

- **WHEN** a key has no stored override
- **THEN** reading it through the settings object returns the instance value

#### Scenario: Override stored

- **WHEN** an override is stored for one key
- **THEN** that key resolves to the override
- **AND** the other keys resolve to the instance values

#### Scenario: Retired key is ignored

- **WHEN** the store holds a row for a key outside the declaration
- **THEN** reading the store does not surface it

#### Scenario: Malformed override is ignored

- **WHEN** the stored override does not parse as the key's type
- **THEN** the key resolves to the instance value

#### Scenario: A cache does not change what resolves

- **WHEN** the same scope and store are resolved with a cache composed and with none
- **THEN** every key resolves to the same value

### Requirement: MCP server selection is a deny-by-default owner setting

The owner settings declaration SHALL include `OUROBOROS_USER_ENABLED_MCP_IDS`, keyed by the owner together with
the agent. An absent stored value SHALL resolve to the instance env value
(`OUROBOROS_USER_ENABLED_MCP_IDS` in the environment, an empty list by default). An empty or malformed
stored value SHALL resolve to an empty list.

A JSON array of non-empty strings and a comma-separated string SHALL resolve to trimmed server ids.
The resolved aggregate SHALL expose the value as `list[str]`.

#### Scenario: Two agents hold independent MCP selections

- **WHEN** an owner stores different MCP server ids for two agents
- **THEN** each accessor resolves the ids stored for its own complete scope

#### Scenario: A missing selection resolves to the instance env default

- **WHEN** no MCP server-id row exists for an agent
- **THEN** the accessor and aggregate both resolve the instance env value (an empty list by default)

#### Scenario: A malformed selection is empty

- **WHEN** the stored MCP server-id value cannot be parsed as a list of non-empty strings
- **THEN** the accessor and aggregate both resolve an empty list

### Requirement: Reasoning visibility is an owner's setting

Whether the model's own text between tool calls is published to the owner's chat SHALL be resolved
through the settings object, so that an owner may hold a value different from the instance default.
The instance default SHALL be to publish it, so that a deployment that sets nothing behaves as it did
before.

The setting SHALL govern that narration only. Messages that report the state of the run — a fallback
notice, a self-check checkpoint, the slow-status line, a timeout notice — SHALL be unaffected: the
owner asked for less of the model's voice, not for the loss of the evidence that work is going on.

The owner SHALL be able to set it from their own chat as well as through the HTTP surface. The chat
command SHALL write the row for the scope it was asked under and SHALL drop that key's cache entry,
so a reader in another process resolves the new value rather than the one it cached.

#### Scenario: An owner who turned it off stops receiving the narration

- **WHEN** the owner's override disables it and a run produces model text beside its tool calls
- **THEN** that text is not published to the owner's chat

#### Scenario: The evidence of work survives the setting

- **WHEN** the owner's override disables it and the run emits a status message
- **THEN** the status message is published as before

#### Scenario: The chat command stores the asker's own override

- **WHEN** an owner asks their chat to hide or show the narration
- **THEN** the row for that owner and agent is replaced with the asked-for value
- **AND** that key's cache entry is dropped

### Requirement: One settings object per process

The system SHALL assemble one settings object when the module is imported and readers SHALL import
it instead of building their own.

Every accessor SHALL take exactly one argument: the identity of the work being done, the owner
together with the agent. A reader SHALL pass the scope it already holds for the work in hand, and
SHALL NOT substitute the instance's configured owner for a scope it was given.

Each accessor is one resolution of its own key, and SHALL NOT take values read elsewhere: no
accessor receives values from its caller, and there is no key map or shared parser through which one
accessor resolves another's key. Where a cache is composed (`user-settings-cache`), that one
resolution is answered from the cache, and the store is read for that same key when it is not — the
reuse is the one method the accessors share, and no caller above the reader supplies it.

#### Scenario: Reader takes the shared object

- **WHEN** the runtime prompt section or the effort level is resolved
- **THEN** it reads the module-level object

#### Scenario: Stored override reaches a reader

- **WHEN** an `OUROBOROS_TIMEZONE` override is stored for the owner's agent
- **THEN** the runtime prompt section reports the overridden timezone

#### Scenario: A reader passes the scope of the work it is doing

- **WHEN** a task runs under one scope and a second task under another
- **THEN** each resolves the overrides of its own scope

#### Scenario: No reader passes a constant owner

- **WHEN** the read sites are inspected
- **THEN** none of them passes the instance's configured owner in place of a scope it holds

#### Scenario: No accessor is handed rows read elsewhere

- **WHEN** the accessors are inspected
- **THEN** none of them takes values from its caller
- **AND** none resolves another accessor's key

### Requirement: Skill selection is an opt-in owner setting

The owner settings declaration SHALL include `OUROBOROS_USER_ENABLED_SKILL_IDS`, keyed by the owner
together with the agent. An absent row SHALL resolve to the instance `AppSettings` value (default
`[]`). Empty text or an empty list SHALL resolve to `[]`, which enables nothing.
A non-list JSON value, malformed list, non-string member, empty member, or non-canonical member SHALL
be an invalid setting; task assembly SHALL translate it to deny-all rather than fall back to a wider
instance selection.

A JSON array of canonical strings and a comma-separated canonical string SHALL resolve to trimmed,
order-preserving, duplicate-free values. The accessor and resolved aggregate SHALL expose the value
as `list[str]`. Parsing SHALL NOT turn display names or legacy short names into skill ids.

#### Scenario: Two owners enable different skills for the same agent id

- **WHEN** two owners store different enabled skill ids under the same agent id
- **THEN** each accessor resolves the allowlist stored for its own complete owner-and-agent scope
- **AND** neither owner reads the other's list

#### Scenario: Two agents of one owner hold independent skill allowlists

- **WHEN** one owner stores different enabled-skill-id lists for two agents
- **THEN** each accessor resolves only the ids stored for its own agent
- **AND** a third agent with no row resolves the instance value

#### Scenario: A missing allowlist resolves to the instance value

- **WHEN** no `OUROBOROS_USER_ENABLED_SKILL_IDS` row exists for an agent
- **THEN** the accessor and aggregate both resolve the instance `AppSettings` value
- **AND** task assembly exposes no skill when that instance value is empty

#### Scenario: An explicit empty allowlist enables nothing

- **WHEN** an owner stores empty text or an empty list while the instance value enables skills
- **THEN** the accessor and aggregate both resolve an empty list
- **AND** task assembly exposes no skill for that invocation

#### Scenario: A malformed allowlist fails closed

- **WHEN** the stored allowlist is not a list of canonical strings or valid comma-separated canonical text
- **THEN** the reader refuses the value
- **AND** task assembly exposes no skill for that invocation

#### Scenario: The parser refuses a legacy alias

- **WHEN** a stored list contains an unqualified display name or legacy skill key
- **THEN** the reader refuses the whole allowlist rather than silently qualifying it
- **AND** the task fails closed for skills

### Requirement: Tool confirmation is an owner's setting

`OUROBOROS_TOOL_CONFIRMATION_ENABLED` SHALL be an owner setting keyed by owner and agent. An absent
stored value SHALL resolve to the instance value; a stored value SHALL be read as the other boolean
keys are. The setting SHALL govern only whether the gate is active; which tools are gated SHALL stay
the security config's.

#### Scenario: An owner's override replaces the instance value

- **WHEN** the instance keeps the gate off and the owner stores `true` for an agent
- **THEN** the accessor and the aggregate both resolve `true` for that owner and agent

#### Scenario: A missing override resolves to the instance value

- **WHEN** no row exists for the key
- **THEN** the accessor and the aggregate both resolve the instance value
