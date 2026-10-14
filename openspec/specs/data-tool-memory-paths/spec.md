## Purpose

The contract between the generic data tools (`data_read`, `data_list`) and memory paths
after the database cutover: which paths are refused and what the refusal says, which paths remain
served, and the guarantee that no memory content is reachable through a generic tool in a way that
bypasses task confinement.

## Requirements

### Requirement: Generic data tools refuse migrated memory paths

`data_read` SHALL refuse any path addressing a capability that now lives in the runtime database —
`memory/global/*`, `memory/scratchpad.md`, and `memory/knowledge/*` — and
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

#### Scenario: Migrated directories are refused by data_list

- **WHEN** the agent calls `data_list` with `memory/knowledge` or `memory/global`
- **THEN** the result is a refusal naming the owning tool
- **AND** no entries derived from database rows or legacy blobs are listed

#### Scenario: Refusal is distinguishable from absence

- **WHEN** a migrated memory path is refused
- **THEN** the result is not the `DATA_NOT_YET_CREATED` sentinel
- **AND** the agent cannot mistake the refusal for an empty or uninitialised capability
