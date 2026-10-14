# task-text-retention Specification

## Purpose

Where a task's text is allowed to live: in the runtime-database records that carry ownership
columns, and never as an object in the storage tier.
## Requirements
### Requirement: A task's text is not persisted as a storage object

The runtime SHALL NOT write a task's text to the object-storage tier. A task's text SHALL live only
in the runtime-database records that carry ownership columns. Where a runtime event would otherwise
carry text too large to record in full, the event SHALL carry a bounded preview, the text's length
and a content hash of it, and SHALL NOT carry a reference to a stored full-text copy.

A scope outside the owner-keyed set produces one key for any two owners, and the scope that routed a
write to the answering instance's own disk no longer exists at all; neither is a place a task's text
may be recovered from. The event row already names the work; the exact prompt is not an artefact
this runtime offers to recover.

#### Scenario: An oversized task text creates no stored object

- **WHEN** a task whose text exceeds the event's truncation threshold is received
- **THEN** no object is created in the object-storage tier
- **AND** the recorded event carries the text's length and its content hash

#### Scenario: The event carries no pointer to a stored copy

- **WHEN** the sanitized task payload of an oversized task is inspected
- **THEN** it carries a truncated preview and is marked truncated
- **AND** it has no field naming a stored full-text object

#### Scenario: A short task text is recorded as before

- **WHEN** a task whose text is below the threshold is received
- **THEN** the event carries the text in full, its length and its content hash
- **AND** it is marked as not truncated

