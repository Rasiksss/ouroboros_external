## ADDED Requirements

### Requirement: Root-owned plans are proposed by subtasks
The root SHALL accept structured proposals only from completed direct children in its scope.
The proposal SHALL distinguish sourced requirements, assumptions, questions and executable steps.

#### Scenario: A table is not an accepted workbook
- **WHEN** a workbook step returns a table or a one-sheet workbook instead of two sheets
- **THEN** the root records failed or inconclusive criteria and the step is not accepted
- **AND** the service does not decide user-task acceptance

### Requirement: Plans and reviews survive process restart
Plan revisions, execution state and reviews SHALL be stored in the runtime database. Writes SHALL
require current scope, root authority and an expected state version.

#### Scenario: A stale proposal cannot replace current work
- **WHEN** a proposal names an obsolete base revision
- **THEN** acceptance is refused without changing the current plan

### Requirement: Completion is distinct from executor success
Root reviews SHALL name result references and observations for each criterion. Failed or
inconclusive criteria SHALL prevent step acceptance. Full completion SHALL require accepted steps;
partial completion SHALL preserve unmet requirements and a reason.
