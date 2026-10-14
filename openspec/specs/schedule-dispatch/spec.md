# schedule-dispatch Specification

## Purpose
Distinguish direct reminder delivery from scheduled agent-task submission so only real tasks cross
the public task and job identity boundaries.

## Requirements
### Requirement: Direct reminders carry no task identity
A scheduled reminder that delivers text directly SHALL publish as an ordinary outbound message with
an empty task identity. The scheduler SHALL NOT expose its slot key as a task or job identifier,
because direct reminder delivery creates no task record.

#### Scenario: A reminder slot fires
- **WHEN** a due schedule with `execution_kind` equal to `reminder` is dispatched
- **THEN** its text is persisted and published under the stored delivery scope
- **AND** the outbound message carries no task identity
- **AND** the slot receipt records the delivery outcome

### Requirement: Scheduled agent tasks retain slot idempotency
A scheduled agent task SHALL submit work through the inbox with a deterministic client message ID
derived from its schedule and slot. Its slot identity SHALL remain distinct from the public job ID
created by task admission.

#### Scenario: An agent-task slot fires
- **WHEN** a due schedule with `execution_kind` equal to `agent_task` is dispatched
- **THEN** it submits one task with client message ID `schedule:<schedule_id>:<slot_id>`
- **AND** task admission remains responsible for the public job ID
