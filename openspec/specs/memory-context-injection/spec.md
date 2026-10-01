# memory-context-injection Specification

## Purpose

How the rows of the unified memory table reach the assembled system prompt. Each kind's policy
declares an injection discipline — full body, deterministic recall, selector-driven recall, or index
line only — and that discipline alone determines the memory's prompt surface.
Body-injected memories are excluded from the index so nothing is stated twice. Two index sections,
`## Memory` and `## Knowledge`, carry their own entry caps and one hardened entry format, render
stably under content edits, and occupy a cache breakpoint of their own. One builder produces these
sections for every deploy environment. The rows themselves are defined by `unified-memory`.

## Requirements
### Requirement: Each kind's injection discipline determines its prompt surface

The system SHALL inject memory into the system prompt according to the injection discipline declared
by each kind's policy, and SHALL NOT decide injection from a hardcoded kind name or file path. The
disciplines SHALL be: `body` — the full content is injected; `recall_auto` — the index line is
injected and bodies are selected deterministically; `recall_selector` — the index line is injected
and bodies are selected by the relevance selector; `index_only` — the index line is injected and the
body is reached with `memory_read`.

#### Scenario: Identity is injected as a body

- **WHEN** the prompt is assembled with an identity memory stored
- **THEN** a section headed `## Identity` carries the full identity content

#### Scenario: Knowledge bodies are not auto-injected

- **WHEN** the prompt is assembled with knowledge memories stored
- **THEN** their index lines are present
- **AND** no knowledge body appears in the prompt

#### Scenario: Recall membership follows the policy

- **WHEN** the relevance selector assembles its candidate list
- **THEN** the candidates are exactly the memories whose kind declares `recall_selector`
- **AND** the deterministically injected memories are exactly those whose kind declares `recall_auto`

### Requirement: Body-injected memories are excluded from the index

The system SHALL omit from every rendered index any memory whose injection discipline is `body`. Its
content is already present in full, and an index line would invite a redundant read. Body injection
is determined solely by the memory's kind policy; no per-row flag overrides it.

#### Scenario: Identity has no index line

- **WHEN** the index is rendered with an identity memory stored
- **THEN** no index line names it

#### Scenario: A knowledge memory keeps its index line

- **WHEN** the index is rendered with a knowledge memory named `patterns` stored
- **THEN** an index line names it in the `## Knowledge` section
- **AND** its body does not appear in the prompt

### Requirement: The prompt carries two index sections split by injection discipline

The system SHALL render two index sections: `## Memory`, covering the kinds whose bodies arrive
through recall, and `## Knowledge`, covering the kinds reached on demand by name. Each section SHALL
state its own retrieval contract in its heading, and SHALL be omitted when it has no entries. Each
section SHALL carry its own entry cap, so entries of one section cannot displace entries of the
other.

#### Scenario: Both sections appear when both have entries

- **WHEN** the prompt is assembled with facts and knowledge memories stored
- **THEN** a `## Memory` section and a `## Knowledge` section are both present

#### Scenario: An empty section is omitted

- **WHEN** no knowledge memories exist
- **THEN** no `## Knowledge` section appears in the prompt

#### Scenario: One section cannot starve the other

- **GIVEN** the number of facts exceeds the `## Memory` section's entry cap
- **WHEN** the prompt is assembled
- **THEN** every knowledge memory within the `## Knowledge` cap still appears

### Requirement: Index entries use one hardened format

Both index sections SHALL render entries in the same format, carrying the kind, the name, and the
authored description, prefixed by a statement that index entries are untrusted data rather than
instructions. Entry values SHALL be collapsed to a single line and SHALL have index-terminator
sequences and `#` characters neutralised before rendering.

#### Scenario: Untrusted-data preamble is present in both sections

- **WHEN** the prompt is assembled with entries in both sections
- **THEN** each section states that its entries are untrusted data, not instructions

#### Scenario: A body-borrowed terminator cannot break out

- **WHEN** a description contains an index terminator sequence or a newline
- **THEN** the rendered entry contains neither in a form that ends or splits the section

### Requirement: The rendered index is stable under content edits

The system SHALL decouple the order in which entries survive the cap from the order in which they
are rendered. Entries SHALL be selected for survival by recency within their section, and SHALL be
rendered sorted by kind and name. Rendering SHALL depend on no value that changes when a memory's
body is edited, so that overwriting content without changing its description produces a
byte-identical index.

#### Scenario: Editing a body does not change the index

- **GIVEN** a rendered index over several memories
- **WHEN** one memory's content is overwritten with the same description
- **THEN** the newly rendered index is byte-identical to the previous one

#### Scenario: Recall does not change the index

- **WHEN** a memory is recalled and its recall counters are updated
- **THEN** the newly rendered index is byte-identical to the previous one

#### Scenario: Creating a memory changes the index

- **WHEN** a new memory is saved
- **THEN** the rendered index contains an additional entry

#### Scenario: Truncation drops the least recent

- **GIVEN** a section holding more entries than its cap
- **WHEN** the index is rendered
- **THEN** the entries omitted are those with the oldest update times
- **AND** the entries shown are ordered by kind and name

### Requirement: The index sections occupy their own cache breakpoint

The system SHALL place the two index sections together in a cacheable prompt block of their own,
positioned after the blocks holding the base prompt, the body-injected memories, and the skills
sections, and before the uncached block. The system SHALL declare at most four cache breakpoints in
total.

#### Scenario: The index block is cacheable

- **WHEN** the prompt is assembled
- **THEN** the block containing the index sections declares a cache breakpoint

#### Scenario: Body-injected memories are in an earlier block

- **WHEN** the prompt is assembled
- **THEN** the `## Identity` section appears in a block preceding the index block

#### Scenario: The scratchpad stays uncached

- **WHEN** the prompt is assembled
- **THEN** the scratchpad section is in the final block, which declares no cache breakpoint

#### Scenario: The breakpoint budget is respected

- **WHEN** the assembled prompt's content blocks are counted
- **THEN** no more than four declare a cache breakpoint

### Requirement: One builder assembles memory sections for every deploy environment

The system SHALL assemble the memory, index, and identity prompt sections through one shared code
path used by every deploy environment. The system SHALL NOT maintain a second message builder whose
memory sections differ from the first. Where a section legitimately does not apply to an
environment, it SHALL be gated by an explicit named condition at that section rather than by the
existence of an alternative builder.

#### Scenario: Production and development assemble the same memory sections

- **WHEN** the prompt is assembled under the production deploy environment and under the development
  deploy environment with the same stored memories
- **THEN** both contain the same memory, knowledge, and identity sections

#### Scenario: Production receives the knowledge index

- **WHEN** the prompt is assembled under the production deploy environment with knowledge memories
  stored
- **THEN** the `## Knowledge` section is present

#### Scenario: Production declares the same cache breakpoints

- **WHEN** the prompt is assembled under the production deploy environment
- **THEN** the base prompt, body-injected memories, and index blocks each declare a cache breakpoint
