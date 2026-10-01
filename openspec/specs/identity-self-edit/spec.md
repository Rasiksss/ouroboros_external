# identity-self-edit Specification

## Purpose

One switch governs, together, whether the agent may rewrite its identity document and whether it
is told that it may — and that switch is hardcoded off, so in every deployment identity is
read-only. The rewrite path it guards stays in the system: it goes through `memory_save` with
`memory_type='identity'`, replaces the document whole under a fixed name, keeps no previous
version of it, and takes effect from the next task. The switch is read through one function,
so the tool schema, the prompt, and the domain cannot disagree. Identity stays undeletable,
single-row, non-empty, and name-reserved at every value; see `unified-memory` and
`identity-memory`.

## Requirements

### Requirement: The self-edit switch is a hardcoded constant

Whether the identity document may be rewritten through the memory tools SHALL be decided by a
single function, and that function SHALL return `false` unconditionally. No environment
variable, settings field, settings file, or tool SHALL be able to change what it returns; the
configuration schema SHALL NOT declare a field for it. Changing the constant SHALL be a code
change.

That function SHALL be the only place the switch influences behavior. Requirements below
stated as "when the switch is on" describe the guarded write path, which SHALL remain
implemented and tested by substituting that function — never by configuration.

#### Scenario: No environment value opens identity for self-edit

- **WHEN** settings are loaded with `OUROBOROS_IDENTITY_SELF_EDIT_ENABLED` set to a true value
- **THEN** the switch is still off
- **AND** the effective policy for kind `identity` has `writable` `false`

#### Scenario: The agent cannot enable the switch itself

- **WHEN** the agent attempts to write `settings.json` through `data_write`
- **THEN** the write is refused

### Requirement: The kind policy is the single source of the identity write permission

The memory domain SHALL expose an effective kind policy that is the static kind policy with
the setting overlaid, changing exactly one field: `writable` on kind `identity`. The
`deletable`, `injection`, and `capacity` fields SHALL NOT be affected by the setting. The
writable-kind list, the write path, the protected-name refusal, and the identity prompt
section SHALL all derive their behavior from this effective policy rather than from separate
checks of the setting.

#### Scenario: The setting is off

- **WHEN** the effective policy for kind `identity` is requested and the setting is off
- **THEN** `writable` is `false`
- **AND** `identity` is absent from the writable-kind list

#### Scenario: The setting is on

- **WHEN** the effective policy for kind `identity` is requested and the setting is on
- **THEN** `writable` is `true` and `deletable` is still `false`
- **AND** `identity` is present in the writable-kind list

### Requirement: Identity is rewritten through memory_save, whole and under a fixed name

When the setting is on, the identity document SHALL be rewritten through the existing
`memory_save` tool with `memory_type='identity'`. No separate identity-writing tool SHALL be
introduced. The write SHALL be refused unless all of the following hold:

- the name is exactly `identity`;
- the mode is `overwrite`;
- the content is non-empty after stripping surrounding whitespace;
- the stripped content is no longer than the configured identity size limit.

The stored description SHALL be the canonical identity description; a description supplied by
the caller SHALL be ignored rather than honoured.

#### Scenario: A valid rewrite replaces the document

- **WHEN** the setting is on and `memory_save(name='identity', memory_type='identity',
  mode='overwrite')` is called with a non-empty body within the size limit
- **THEN** the write succeeds
- **AND** reading the identity document returns the new body

#### Scenario: Append mode is refused

- **WHEN** the setting is on and an identity write is attempted with `mode='append'`
- **THEN** the write is refused
- **AND** the identity document is unchanged

#### Scenario: An empty or oversized body is refused

- **WHEN** the setting is on and an identity write is attempted with a body that is empty
  after stripping, or longer than the size limit
- **THEN** the write is refused, naming the limit
- **AND** the identity document is unchanged

#### Scenario: A second identity row cannot be created

- **WHEN** the setting is on and a write is attempted with `memory_type='identity'` under a
  name other than `identity`
- **THEN** the write is refused

### Requirement: A rewrite keeps no previous version

Identity SHALL be stored as exactly one memory. A rewrite SHALL NOT archive, copy, or version
the body it replaces, and after any number of rewrites exactly one row of kind `identity` SHALL
exist, named `identity`, at any status. The replaced text is not recoverable through the tools;
the audit log record is the only trace a rewrite leaves.

An identity write SHALL refuse a body that opens with the rendered memory header — the
frontmatter the read tool rebuilds, ahead of which it puts the time labels and the freshness
note — instead of stripping it. Stripping would silently truncate a document whose own
frontmatter used the same shape, and no previous version exists to restore from; the refusal
names the fix instead. A body that opens with a delimiter block that is not a rendered memory
header SHALL be stored unchanged. A body that is nothing but the rendered header SHALL be
refused.

#### Scenario: Repeated rewrites leave one row and no copy

- **WHEN** the setting is on and identity is rewritten more than once
- **THEN** exactly one memory of kind `identity` exists, named `identity`
- **AND** no archived copy of any replaced body is present in the audit listing

#### Scenario: Writing back what the read tool returned is refused

- **WHEN** the text returned by reading `identity` is passed unedited as the body of an
  identity write
- **THEN** the write is refused with a message telling the caller to send the document body
  only
- **AND** the identity document is unchanged

#### Scenario: A body with a leading block of its own is stored unchanged

- **WHEN** an identity write is given a body that opens with a delimiter block that is not a
  rendered memory header
- **THEN** the body is stored exactly as supplied

#### Scenario: A body that is nothing but the rendered header is refused

- **WHEN** an identity write is given a body consisting only of a rendered memory header
- **THEN** the write is refused
- **AND** the identity document is unchanged

### Requirement: Automated distillation may never write identity

The identity kind SHALL be forbidden to the automated distillation caller regardless of the
setting. A distillation caller SHALL NOT be offered `identity` in the write tool's kind enum,
and a direct attempt SHALL be refused with the message used for kinds withheld from
distillation.

The permitted and the withheld lists rendered into the distillation prompt SHALL both derive
from the same functions the tool schema reads, so a kind reaches the prompt and the schema
together.

The withheld list SHALL name only kinds some caller may actually write. A kind offered to no one
is not withheld from distillation — it is absent from the deployment, and naming it would
describe an option that does not exist. `identity` SHALL therefore be named as withheld only
while the setting is on, even though distillation may never write it at either value.

Each kind the withheld list names SHALL carry a reason of its own and SHALL NOT inherit one
written for another kind. In particular the prompt SHALL NOT describe `identity` as a record of
the user, nor as reserved for regular `memory_save`.

#### Scenario: A kind no caller may write is not named as withheld

- **WHEN** the distillation prompt is built with the setting off
- **THEN** the withheld list names `user` and `feedback`
- **AND** it does not name `identity`

#### Scenario: Each withheld kind carries its own rationale

- **WHEN** the distillation prompt is built with the setting on
- **THEN** every kind the withheld list names is given a reason of its own
- **AND** the reason given for `identity` does not describe it as a record of the user

#### Scenario: Distillation is not offered identity

- **WHEN** the write tool's schema is built for a distillation caller and the setting is on
- **THEN** `identity` is absent from the `memory_type` enum

#### Scenario: A direct distillation write is refused

- **WHEN** a distillation caller invokes the write tool with `memory_type='identity'` and the
  setting is on
- **THEN** the call is refused with the message stating the kind cannot be set by automated
  distillation

### Requirement: The tool surface advertises identity only when it is writable

The `memory_type` enum, the kind hint, and the write tool's description SHALL be built per
caller. They SHALL mention identity only when the caller may actually write it. When identity
is offered, the description SHALL state that the document is replaced in full, that the name
and mode are fixed, that the caller's description is ignored, that no copy of the replaced text
is kept, and that the new text enters the prompt from the next task.

A successful identity write SHALL return a message stating that the new text takes effect from
the next task, and that the text it replaced is not kept. It SHALL NOT offer a rollback.
Identity writes SHALL be recorded with a provenance value distinguishing them from ordinary
saves.

#### Scenario: The setting is off

- **WHEN** the write tool's schema and texts are built for a regular caller and the setting is
  off
- **THEN** `identity` is absent from the `memory_type` enum
- **AND** neither the kind hint nor the tool description mentions writing identity
- **AND** a direct attempt is refused with the "must be one of" message listing the permitted
  kinds

#### Scenario: The setting is on

- **WHEN** the write tool's schema and texts are built for a regular caller and the setting is
  on
- **THEN** `identity` is present in the `memory_type` enum
- **AND** the description states the fixed name and mode, that no copy is kept, and the
  next-task effect

#### Scenario: A successful rewrite tells the agent what happened

- **WHEN** an identity rewrite succeeds
- **THEN** the tool result states that the new text enters the prompt from the next task
- **AND** it states that the replaced text is not kept, naming no memory to roll back to

### Requirement: The identity prompt section states its own writability

The `## Identity` prompt section header SHALL be derived from the same function that governs
the write permission. When identity is not writable, the header SHALL state that the document
is read-only and that no tool can change it. When it is writable, the header SHALL state how
to rewrite it, that no previous version is kept, that the new text reaches the prompt from the
next task, and that this should be done rarely and deliberately. The identity body SHALL
be injected in full in both cases.

The system prompt SHALL NOT assert unconditionally that identity cannot be rewritten. It SHALL
continue to assert that identity can never be deleted, and SHALL defer the question of
rewriting to the `## Identity` block itself.

#### Scenario: Read-only header

- **WHEN** the prompt is assembled with the setting off
- **THEN** the `## Identity` header states the document is read-only
- **AND** it does not name the write tool

#### Scenario: Writable header

- **WHEN** the prompt is assembled with the setting on
- **THEN** the `## Identity` header names the write call, states that no previous version is
  kept, and states the next-task effect

#### Scenario: The system prompt no longer forbids rewriting outright

- **WHEN** the system prompt's memory-kind list is read
- **THEN** it states that identity can never be deleted
- **AND** it does not state unconditionally that identity cannot be written

### Requirement: Identity rewrites are logged without the body

An identity rewrite SHALL emit a log record carrying the body size before and after, a short
hash prefix of the new body, and the task id, type, and chat id. The document body SHALL NOT
be written to the log.

The sizes and the hash SHALL describe the document as stored, not the content the write was
handed: the store normalizes the body before persisting it, so measuring the argument would
report a size and a hash for a state that never existed in the database. Both sizes SHALL be
measured on stored bodies, so that they are comparable with each other.

The record is emitted after the write has committed, so a failure to emit it SHALL NOT be
reported as a failed save. Reporting one would prompt a retry of a rewrite that already landed.
With no previous version kept, this record is the only trace that the document changed.

#### Scenario: A rewrite whose audit line cannot be emitted still reports success

- **WHEN** an identity rewrite commits and emitting its log record fails
- **THEN** the call reports success and the rewrite stands

#### Scenario: A rewrite is observable in the log

- **WHEN** an identity rewrite succeeds
- **THEN** a log record reports the sizes, the hash prefix, and the task id, type, and chat id
- **AND** the size and hash it reports are those of the stored document
- **AND** it does not contain the document body
