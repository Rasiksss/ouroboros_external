# prompt-assembly Specification

## Purpose

How the system prompt is assembled from its sections, and how it is shrunk when it exceeds the soft
cap. Every section is declared once — its name, heading, cache block, builder, byte budget, drop
priority, and truncation — in a single ordered registry that assembly walks and trimming sorts, so
the two cannot disagree. Shrinking happens while the sections are still separate values, before they
are joined into content blocks, so a section boundary is never recovered by parsing assembled text
and free-form content can never start, end, or absorb a section. The mechanism — the declaration
record, the walk, and the shrink algorithm — is runtime-independent and lives in `new_core/prompt`;
which sections exist and what they contain is `ouroboros/agent/context.py`'s, the same way the task
graph's topology is data supplied to the engine in `graph-core`.

This capability owns the assembly mechanism, not the content. Which memory reaches which prompt
surface is `memory-context-injection`, which owns the knowledge section as well — its index render
and its injection; the recent chat tail is `dialogue-context-assembly`; the scratchpad and working
retrieval sections are `scratchpad-memory` and `working-retrieval`. Cache-block placement and the
four-breakpoint budget are required by `memory-context-injection` and are preserved here rather than
restated.
## Requirements

### Requirement: Every prompt section is declared once

The system SHALL declare each system-prompt section exactly once, in a single ordered registry of
records. Each record SHALL carry that section's name, its heading, the cache block that holds it, the
builder that produces its text, and — where it is not protected — the drop priority and minimum size
that govern how it shrinks. The system SHALL NOT maintain a second list that names sections, orders
them, or classifies them for trimming.

A section's name SHALL be the one identity used wherever that section is referred to: as its byte
budget's key, as its drop key, and as its label in trimming logs. The system SHALL NOT key one of
those by name and another by heading text.

#### Scenario: The registry is the only place sections are enumerated

- **WHEN** the set of prompt sections, their order, or their drop classification is needed
- **THEN** it is read from the section registry
- **AND** no separate prefix list, drop-order list, or protection set exists alongside it

#### Scenario: A section absent from the registry cannot reach the prompt

- **WHEN** a builder produces text that no registry record declares
- **THEN** that text is not assembled into the system prompt

#### Scenario: One name serves budget, drop order, and logging

- **WHEN** a section is over its byte budget and later trimmed by the soft cap
- **THEN** the budget warning and the trimming record identify it by the same declared name

### Requirement: The assembly mechanism is runtime-independent; the prompt's sections are not

The system SHALL keep the section declaration record, the cache-block ordering, the builder and
truncation callable contracts, and the assemble-and-shrink walk free of any dependency on this
runtime's storage, settings, or prompt content. That mechanism SHALL be exercisable without a drive,
a database, or a configured owner.

Which sections exist, their order, their budgets, their drop priorities, and every builder that
produces section text SHALL remain in the agent layer and SHALL be supplied to the mechanism as data,
in the same way the task graph's topology is supplied to the graph engine. The mechanism SHALL NOT
name any individual section, and SHALL NOT be given a way to distinguish one section's subject from
another's.

A section's builder SHALL be supplied as a callable over a caller-defined inputs type that the
mechanism does not inspect, rather than as an implementation of a mechanism-side interface per
section.

#### Scenario: The mechanism runs with no runtime present

- **WHEN** the assemble-and-shrink walk is exercised with declarations whose builders return fixed
  text
- **THEN** it produces the assembled blocks and trimming record without reading storage, settings, or
  any owner state

#### Scenario: The mechanism names no section

- **WHEN** the assembly mechanism is inspected
- **THEN** it contains no reference to any particular section's heading, subject, or content

#### Scenario: Section inputs stay opaque to the mechanism

- **WHEN** a builder is invoked with the caller's inputs
- **THEN** the mechanism passes them through without inspecting them

### Requirement: Recalled memories cross the assembly boundary as a named shape

The system SHALL carry a recalled memory between durable-memory recall and the prompt section that
renders it as a named shape whose fields are addressed by name. The system SHALL NOT pass recalled
memories between those points as positional tuples whose meaning is recoverable only from a
docstring.

#### Scenario: Recall returns named records

- **WHEN** durable-memory recall returns the memories selected for a task
- **THEN** each carries its content, its first-recorded time, and its updated time as named fields

#### Scenario: The section builder reads fields by name

- **WHEN** the recalled-memories section is rendered
- **THEN** it addresses each memory's content and timestamps by name rather than by position

### Requirement: Trimming operates on declared sections, never on reparsed text

The system SHALL apply the context soft cap to sections while they are still separate values, before
they are joined into the prompt's content blocks. The system SHALL NOT recover section boundaries by
parsing assembled prompt text back apart by heading.

Because boundaries are carried rather than re-derived, free-form section content SHALL NOT be able to
create, end, or absorb a section. Text that a user wrote into a section body is content, never
structure, whatever it looks like.

#### Scenario: A heading typed into free-form content starts no section

- **WHEN** a user's scratchpad body contains a line that reads as another section's heading
- **THEN** that line stays inside the scratchpad section's content
- **AND** it neither starts a new section nor ends the scratchpad section

#### Scenario: A protected section is not absorbed by its neighbour

- **WHEN** a protected section immediately follows a section with free-form content
- **THEN** it remains its own section and is trimmed according to its own declaration

#### Scenario: Trimming precedes joining

- **WHEN** the estimated prompt exceeds the soft cap
- **THEN** sections are dropped or truncated before the prompt's content blocks are assembled

### Requirement: Protection and drop order follow from the declaration

A section SHALL be protected from the soft cap exactly when its record declares no drop priority, and
SHALL be droppable exactly when it declares one. The system SHALL NOT express protection as a
separate set of headings that can disagree with the drop order.

The soft cap SHALL consider droppable sections in ascending order of declared drop priority, and
SHALL stop as soon as the prompt fits. A section declaring a minimum size SHALL be truncated to that
size before it is considered for removal; a section declaring none SHALL be removed whole.

#### Scenario: A section with no drop priority is never trimmed

- **WHEN** the soft cap runs and the prompt is over budget
- **THEN** no section whose record declares no drop priority is dropped or truncated

#### Scenario: Drop priority determines the order

- **WHEN** two droppable sections could each free enough space
- **THEN** the one with the lower declared drop priority is acted on first

#### Scenario: A minimum size means truncate before dropping

- **WHEN** a droppable section declaring a minimum size is selected and is larger than it
- **THEN** it is truncated to that size and kept, rather than removed

#### Scenario: Sections in a cached block declare no drop priority

- **WHEN** the registry is inspected
- **THEN** sections placed in a cache-controlled block declare no drop priority, so the soft cap
  cannot shrink a cached prefix

### Requirement: A section with internal structure owns its own truncation

A section whose content has internal structure that must survive shrinking SHALL declare its own
truncation, and that truncation SHALL operate on the parts its builder already holds. The system
SHALL NOT recover a section's internal structure by pattern-matching over text the same module
produced.

Where a section declares no truncation of its own, the system SHALL truncate it by dropping whole
leading units at its declared unit delimiter, and SHALL record how much was omitted.

#### Scenario: The recent chat section divides its own budget

- **WHEN** the recent chat section is truncated
- **THEN** its committed summary and its fresh tail are bounded from the parts its builder holds
- **AND** its data-block delimiters remain balanced and its backlog marker is not taken from content

#### Scenario: A section without declared truncation keeps its tail

- **WHEN** a droppable section declaring only a unit delimiter is truncated
- **THEN** whole leading units are dropped until it fits and the omitted size is recorded

### Requirement: The assembled prompt is unchanged by how sections are declared

The system SHALL assemble the declared sections in registry order and apply the same soft cap to identical complete inputs deterministically. Removed capabilities SHALL have no section or empty cache block.

The registry SHALL preserve the cache-block placement and breakpoint budget that
`memory-context-injection` requires; it SHALL NOT restate them.

#### Scenario: Section text is byte-identical

- **WHEN** the prompt is assembled from identical complete inputs, including stored memories,
  scratchpad, chat tail, runtime state, and current time
- **THEN** each remaining section's text is byte-identical across repeated builds

#### Scenario: Block placement and breakpoints are preserved

- **WHEN** the assembled prompt's content blocks are counted
- **THEN** the base prompt, skills, and nonempty index blocks each declare a cache
  breakpoint, no more than four declare one, and the volatile block declares none

#### Scenario: The same soft cap trims the same sections

- **WHEN** the soft cap is exceeded by the same margin over the same sections
- **THEN** the same sections are dropped or truncated, and the reported trimming record names them

### Requirement: Active instructions are a protected run section

The system SHALL declare `active_skill_instructions` in the existing registry: a dynamic volatile
block after the permanent blocks of the system prompt, with no cache breakpoint, drop priority,
fallback, or truncation, accounted for in context estimation. An update SHALL preserve late system
notes and the current history, SHALL NOT repeat discovery, and SHALL NOT recover boundaries by
parsing Markdown. An empty activation SHALL preserve the prior prompt and cache breakpoints.

Active skill instruction rendering and overflow handling are owned by `skill-activation`. This spec
owns the section declaration and budget mechanics.

#### Scenario: Empty and activated prompt under a soft cap

- **WHEN** a new run assembles the prompt, then accepts pages while the soft cap is in effect
- **THEN** before reading the block is absent; after reading the other sections compress per their
  declarations, and late notes and history are preserved

#### Scenario: Stable render order and cache prefix

- **WHEN** an existing skill is re-read with sufficient budget or the prompt is reassembled without
  new events
- **THEN** the system prompt is byte-identical; when the projection shrinks, selected sections remain
  in first-activation order after the permanent prefix

### Requirement: No environment-profile document exists

The system SHALL NOT generate, store, read, or inject an environment-profile document. No
environment-profile section SHALL appear in the assembled system prompt, no environment-profile
check SHALL appear in startup verification, and no code path SHALL write such a document. Any
environment-profile object left over from an earlier version SHALL be ignored rather than read or
deleted.

#### Scenario: Fresh boot writes no environment profile

- **WHEN** the agent starts against an empty data root
- **THEN** no `memory/WORLD.md` object is created on any backend

#### Scenario: Prompt carries no environment-profile section

- **WHEN** the system prompt is assembled
- **THEN** it contains no `## Environment Profile` section

#### Scenario: A leftover environment profile is inert

- **GIVEN** `memory/WORLD.md` exists from an earlier version
- **WHEN** the system prompt is assembled and startup verification runs
- **THEN** its content appears in neither
- **AND** the object is not deleted

#### Scenario: Startup verification omits the environment-profile check

- **WHEN** startup verification runs
- **THEN** the emitted verification event contains no environment-profile check
- **AND** a missing environment profile contributes nothing to the issue count
