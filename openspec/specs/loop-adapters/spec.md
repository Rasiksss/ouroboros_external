# loop-adapters Specification

## Purpose

How the ouroboros side of the loop graph is organized. Each adapter implementing a
`new_core.loop` interface owns the helpers only it calls; helpers shared between owners live
in one named module (`new_core.loop.transcript_merge`, shared with the loop's own nodes);
nothing on the loop path is reached through call-time module-object resolution, and the
legacy `ouroboros/agent/loop.py` module does not exist. The seam tests use to fake a loop
effect is the adapters' interface methods — the same boundary the graph nodes call.

## Requirements

### Requirement: Each adapter owns the helpers only it calls

Every ouroboros-side helper of the loop graph SHALL live with its production caller: a helper
called by exactly one adapter SHALL be a private method of that adapter, and the ouroboros
wiring of the loop SHALL NOT keep a residual module of homeless loop functions. The legacy
`ouroboros/agent/loop.py` module SHALL NOT exist.

#### Scenario: Ownership is readable from the adapter

- **WHEN** a reader opens the adapter implementing a loop interface (compactor, LLM provider,
  event sink, owner inbox, tool runner)
- **THEN** the implementation of each interface method is a method of that adapter (or an
  object the adapter constructed), not a call into a separate loop module

#### Scenario: The legacy module is gone

- **WHEN** the repository is searched for `ouroboros.agent.loop` imports or the module file
- **THEN** neither exists

### Requirement: No call-time module-object resolution on the loop path

Adapters and the task-graph builder SHALL reach their helpers by direct import or method
call. They SHALL NOT resolve a function through a module object at call time
(`import module; module.helper(...)` inside the call) as a patching seam. The seam offered to
tests SHALL be the adapters' interface methods themselves.

#### Scenario: A test fakes a loop effect at the graph boundary

- **WHEN** a test needs to fake owner-message draining, the self-check checkpoint, transcript
  sealing, compaction, or the LLM call
- **THEN** it patches the corresponding adapter class method (`drain`, `maybe_checkpoint`,
  `seal`, `compact`, `call`, …)
- **AND** no test patches a loop-module attribute to intercept an adapter's delegation

### Requirement: Shared transcript-shape helpers live in one named module

Helpers used by more than one owner to mutate the OpenAI-shape transcript — appending or
merging user content without consecutive same-role turns, and merging a note into the system
message — SHALL live in a single shared module with public names, imported by every consumer.
They SHALL NOT be duplicated per adapter. Because the loop's own nodes inject user turns too
and `new_core/loop` may not import `ouroboros`, that module SHALL sit on the `new_core` side
(`new_core/loop/transcript_merge.py`), the only layer every consumer can import.

#### Scenario: Two adapters merge user turns identically

- **WHEN** the event-sink adapter injects a self-check turn and the owner-inbox adapter
  injects an owner message
- **THEN** both go through the same shared merge function
- **AND** multipart content blocks and merge separators behave identically on both paths

#### Scenario: The loop layer and the adapters merge through one implementation

- **WHEN** a loop node injects a user turn (the skill-finalization reminder, the round-limit
  notice) and an adapter injects one on the ouroboros side
- **THEN** both call the same function in `new_core.loop.transcript_merge`
- **AND** no second implementation of the merge rules exists in the repository for the loop
  layer to keep in step by hand
