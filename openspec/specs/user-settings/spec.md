## Purpose

Per-owner overrides of the settings a user is allowed to change. Overrides live in the sparse
runtime-database table `user_settings`, where the absence of a row means the instance value
applies. Everything else in `AppSettings` stays instance-only: those keys decide where data goes,
which credentials sign it, and how the deployment behaves as a whole. One `user_settings` object is
assembled when the module is imported and readers import it; nothing builds its own or resolves
settings implicitly. Accessors take the owner as an argument. Owner resolution does not exist yet,
so every reader passes the single `global` owner; `owner_id` lives on in the table column and the
interfaces. Writing overrides has no surface yet.

## Requirements

### Requirement: A key resolves to the stored override, else the instance value

The system SHALL resolve each key in `USER_SETTING_KEYS` to the override stored for the owner
passed to the accessor when one exists, and to the instance value otherwise. Rows for keys outside
`USER_SETTING_KEYS` SHALL be ignored when the table is read. Overrides are stored as text; an
override that does not parse as the key's type SHALL resolve to the instance value.

#### Scenario: No override stored

- **WHEN** a key has no stored override
- **THEN** reading it through `UserSettings` returns the instance value

#### Scenario: Override stored

- **WHEN** an override is stored for one key
- **THEN** that key resolves to the override
- **AND** the other keys resolve to the instance values

#### Scenario: Retired key is ignored

- **WHEN** the table holds a row for a key outside `USER_SETTING_KEYS`
- **THEN** reading the table does not surface it

#### Scenario: Malformed override is ignored

- **WHEN** the stored override does not parse as the key's type
- **THEN** the key resolves to the instance value

### Requirement: Overrides are read once per process

The system SHALL read the table through the store once and serve later reads from the cache. An
owner without overrides SHALL be cached as an empty set of overrides, distinct from a cache miss,
so the table is not re-read.

#### Scenario: Later reads hit the cache

- **WHEN** several keys are read
- **THEN** the store is asked once

#### Scenario: No overrides is an answer

- **WHEN** the owner has no rows and several keys are read
- **THEN** the store is asked once

### Requirement: One settings object per process

The system SHALL assemble one settings object when the module is imported and readers SHALL import
it instead of building their own. Until owner resolution exists every reader SHALL pass
`SETTINGS_OWNER_ID`.

#### Scenario: Reader takes the shared object

- **WHEN** the runtime prompt section or the effort level is resolved
- **THEN** it reads the module-level object with `SETTINGS_OWNER_ID`

#### Scenario: Stored override reaches a reader

- **WHEN** an `OUROBOROS_TIMEZONE` override is stored for the owner
- **THEN** the runtime prompt section reports the overridden timezone
