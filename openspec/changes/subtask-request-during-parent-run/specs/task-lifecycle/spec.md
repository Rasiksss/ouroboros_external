## ADDED Requirements

### Requirement: A subtask request reaches the supervisor when `schedule_task` returns

A run that schedules a child task SHALL hand the scheduling request to the supervisor through the run's
event publisher once the call has written the child's `requested` record, so that the child can start while
its parent is still running. A request handed over this way SHALL NOT also be reported with the run's
result: each request is delivered at most once. When the publisher is absent or refuses the request, or when
the run is a durable job, or when the record could not be written, the request SHALL be reported with the
run's result as before. The order is fixed: the record exists before the supervisor can hear of the child.

#### Scenario: The child is requested while the parent runs

- **WHEN** a running task calls `schedule_task` and its record write succeeds
- **THEN** the scheduling request is published before the call returns
- **AND** the task's pending events do not contain it

#### Scenario: The record is written before the request is published

- **WHEN** the publisher receives the scheduling request
- **THEN** the child's task record already has status `requested`

#### Scenario: A refusing transport keeps the request with the run

- **WHEN** the event publisher raises while the request is handed over
- **THEN** the call still succeeds
- **AND** the request stays in the task's pending events and leaves with the run result

#### Scenario: A durable job reports its children with its result

- **WHEN** a durable job calls `schedule_task`
- **THEN** nothing is published during the call
- **AND** the request stays in the job's pending events
