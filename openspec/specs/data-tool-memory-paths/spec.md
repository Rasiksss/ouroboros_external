## Purpose

The contract between the generic data tools (`data_read`, `data_list`) and memory paths
after the database cutover: which paths are refused and what the refusal says, which paths remain
served, and the guarantee that no memory content is reachable through a generic tool in a way that
bypasses task confinement.

## Requirements

### Requirement: Generic data tools refuse migrated memory paths

`data_read` SHALL refuse any path addressing a capability that now lives in the runtime database —
`memory/global/*`, `memory/scratchpad.md`, `memory/knowledge/*`, and `memory/identity.md` — and
SHALL NOT return their content, neither from the database nor from the legacy blob left on the
storage backend. The refusal SHALL name the tool that owns the data. `data_list` SHALL refuse the
corresponding directories in the same way.

#### Scenario: Long-term memory path is refused

- **WHEN** the agent calls `data_read` with `memory/global/user_role.md`
- **THEN** the result is a refusal naming `memory_read`
- **AND** the content of the legacy blob at that path is not returned

#### Scenario: Scratchpad path is refused

- **WHEN** the agent calls `data_read` with `memory/scratchpad.md`
- **THEN** the result is a refusal naming `update_scratchpad`
- **AND** the rendered scratchpad view is not returned

#### Scenario: Knowledge topic path is refused and names the memory tool

- **WHEN** the agent calls `data_read` with `memory/knowledge/patterns.md`
- **THEN** the result is a refusal naming `memory_read`
- **AND** neither the database row nor the legacy blob is returned

#### Scenario: Identity path is refused

- **WHEN** the agent calls `data_read` with `memory/identity.md`
- **THEN** the result is a refusal naming `memory_read`
- **AND** neither the database row nor the legacy blob is returned

#### Scenario: Migrated directories are refused by data_list

- **WHEN** the agent calls `data_list` with `memory/knowledge` or `memory/global`
- **THEN** the result is a refusal naming the owning tool
- **AND** no entries derived from database rows or legacy blobs are listed

#### Scenario: Refusal is distinguishable from absence

- **WHEN** a migrated memory path is refused
- **THEN** the result is not the `DATA_NOT_YET_CREATED` sentinel
- **AND** the agent cannot mistake the refusal for an empty or uninitialised capability

### Requirement: Confinement cannot be bypassed through memory paths

No generic data tool SHALL return content sourced from a process-wide memory service while the
calling task is under a payload confinement constraint. Where a confined path resolves, the
resolved target SHALL be the only source consulted.

#### Scenario: Confined task cannot read global knowledge

- **WHEN** a task with a `skill_repair` constraint and a payload root calls `data_read` with
  `memory/knowledge/patterns.md`
- **THEN** the global knowledge topic is not returned

#### Scenario: Confined task cannot read the global scratchpad

- **WHEN** a task with a `skill_repair` constraint and a payload root calls `data_read` with
  `memory/scratchpad.md`
- **THEN** the global scratchpad view is not returned

#### Scenario: Resolved confined target is never discarded

- **WHEN** `data_read` resolves a payload-confined target for any path
- **THEN** the content returned is read from that target or the call is refused
- **AND** no branch substitutes a globally scoped source
