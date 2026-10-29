## ADDED Requirements

### Requirement: Semantic duplicate detection excludes a child's ancestors

When admitting a subtask, the supervisor SHALL exclude its parent and transitive ancestors belonging
to the same owner and agent from semantic duplicate candidates. Siblings and unrelated active tasks
SHALL remain candidates. Traversal SHALL stop at a missing record, foreign owner/agent, or cycle.

#### Scenario: A planner repeats its parent's request

- **WHEN** a child requests a plan for its running parent's original user request
- **THEN** neither the parent nor its ancestors are offered to the duplicate classifier
- **AND** the child can be queued while its parent waits

#### Scenario: A second planner duplicates an active sibling

- **WHEN** the classifier identifies a sibling as a duplicate
- **THEN** admission is rejected with `rejected_duplicate`

### Requirement: A rejected child has a correlated failure outcome

When rejecting a child as a duplicate, the supervisor SHALL publish its failure with the child's
task ID, original owner scope, source and transport. Its text SHALL use the existing subtask failure
prefix. Suppressing admission acknowledgements SHALL NOT suppress this terminal outcome.

#### Scenario: A client waits for a rejected child

- **WHEN** a requested child is rejected as a duplicate
- **THEN** the client receives a failure outcome naming that child rather than an anonymous notice
- **AND** its stored status remains `rejected_duplicate`
