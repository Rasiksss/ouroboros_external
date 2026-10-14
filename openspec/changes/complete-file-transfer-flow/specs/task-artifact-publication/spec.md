## ADDED Requirements

### Requirement: Task artifact fallback precedes filename matching
Artifact resolution SHALL prefer an accessible exact ID or stored path, followed by the current task's artifact path, followed by an accessible unique display name.

#### Scenario: Two tasks publish the same filename
- **WHEN** successive tasks publish report.txt and the second task references uploads/report.txt
- **THEN** the second task's bytes are resolved
- **AND** an explicit ID or full path still resolves the file it names

#### Scenario: An exact flat artifact path exists
- **WHEN** a flat report.txt artifact and a current-task report.txt artifact both exist
- **THEN** uploads/report.txt resolves the exact flat artifact path
