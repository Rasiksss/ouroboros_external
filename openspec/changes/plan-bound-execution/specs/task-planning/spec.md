## ADDED Requirements

### Requirement: Accepted steps govern executor dispatch
Root executor calls under an accepted plan SHALL name revision, step and attempt. The runtime
SHALL persist their identity and validate dependencies before dispatch, bind scheduled children
before publishing them, and prevent closing attempts while an executor or child is active.
Semantic result acceptance SHALL remain the root orchestrator's decision.

#### Scenario: An unbound author cannot start
- **WHEN** a root schedules a code author without a valid plan binding
- **THEN** dispatch refuses before any child request is published
- **AND** a valid binding starts the named step and retains the actual child task id

### Requirement: Cancellation leaves the current run immediately
A cancellation request SHALL be sent through the live event channel without waiting for the
requesting root to finish. Fallback delivery SHALL be retained only if that channel declines it.

#### Scenario: A root waits after cancellation
- **WHEN** the root requests cancellation and continues waiting
- **THEN** the supervisor receives the cancellation during that run

### Requirement: Ready artifact identities remain discoverable
AIFA SHALL support bounded artifact-index pages and type filters, with a next-page offset.
Compact values SHALL be available without loading complete provenance; the full view remains.

#### Scenario: A narrative exists beyond the first page
- **WHEN** a caller asks for NarrativeOutput after a long run
- **THEN** its id can be read without repeating that run
