# consolidation-scheduling Specification

## Purpose

When the supervisor may enqueue a `context_consolidation` task. Two paths ask for a
consolidation — a persisted chat message and a finished run — and both funnel through
`TaskQueue.schedule_context_consolidation`, so one rule covers every caller.
Beyond the backlog thresholds and the single-flight guard, that funnel is where a failing
consolidation is kept from turning into an unbounded retry loop: rescheduling after a run is
decided by the run's outcome, and consecutive failures earn a growing, capped cooldown. What a
consolidation run itself does — the summary transition, the cursor compare-and-swap, the
continuation — is not specified here.

## Requirements

### Requirement: Only a completed consolidation run may schedule the next one

After a `context_consolidation` run reaches a terminal outcome, the supervisor SHALL schedule a
follow-up run only when that outcome is `completed`. A `failed` outcome SHALL NOT schedule a
follow-up, whether or not the chat tail moved during the run.

A `context_consolidation` task killed by the hard timeout SHALL NOT schedule a follow-up. A kill is a
failed run.

A run that ends with a continuation SHALL keep committing as `interrupted` and returning before the
scheduling decision, unchanged.

#### Scenario: A failed run in an active chat schedules nothing

- **WHEN** a consolidation run fails
- **AND** new chat messages arrived while it was running
- **THEN** no follow-up consolidation task is enqueued by the run's own completion path

#### Scenario: A completed run may schedule the next one

- **WHEN** a consolidation run completes
- **THEN** the follow-up scheduling check runs as before, subject to thresholds and single-flight

#### Scenario: A hard-timed-out consolidation schedules nothing

- **WHEN** a `context_consolidation` task is killed by the hard timeout and not requeued
- **THEN** no follow-up consolidation task is enqueued

#### Scenario: A continuation is unaffected

- **WHEN** a run yields a continuation
- **THEN** it commits as `interrupted` and the scheduling decision is not reached

### Requirement: Consecutive consolidation failures throttle scheduling

The supervisor SHALL keep a count of consecutive failed consolidation runs in owner runtime state,
alongside the timestamp of the most recent failure. The count SHALL be incremented on a `failed`
outcome, including a hard-timeout kill, and reset to zero on a `completed` outcome.

`schedule_context_consolidation` SHALL decline to enqueue while a cooldown derived from that count has
not elapsed since the last failure, and SHALL apply this check to every caller — message-driven,
run-driven, and timeout-driven alike. The cooldown SHALL grow with the failure count and SHALL be
capped, so a persistently failing consolidation settles into occasional retries rather than either a
loop or a permanent stall.

Absent state SHALL read as zero consecutive failures.

#### Scenario: A failing consolidation with an active chat creates one task, not a stream

- **WHEN** consolidation fails on every attempt and chat messages keep arriving
- **THEN** scheduling is declined for the duration of the cooldown
- **AND** the number of consolidation tasks created stays bounded rather than growing with message
  volume

#### Scenario: The cooldown grows with consecutive failures

- **WHEN** consolidation fails repeatedly
- **THEN** each successive cooldown is longer than the previous one, up to the cap

#### Scenario: A success reopens the gate immediately

- **WHEN** a consolidation run completes after one or more failures
- **THEN** the consecutive-failure count is reset to zero
- **AND** the next scheduling attempt is not throttled

#### Scenario: The cooldown expires and one attempt is allowed

- **WHEN** the cooldown for the current failure count has elapsed
- **AND** the thresholds and the single-flight guard allow it
- **THEN** a consolidation task is enqueued

#### Scenario: A deployment with no stored counter is not throttled

- **WHEN** owner runtime state holds no consolidation failure record
- **THEN** scheduling proceeds as if there had been no failures

### Requirement: Throttled consolidation is visible in the logs

A declined schedule SHALL be logged at info level with the consecutive-failure count and the
remaining cooldown. A consolidation that has been failing for a long time SHALL be readable from the
logs without correlating task rows. The level is info rather than warning because every persisted
message asks, so the same unchanged fact repeats for the whole cooldown.

#### Scenario: A declined schedule says why

- **WHEN** scheduling is declined because the cooldown has not elapsed
- **THEN** an info line is logged naming the consecutive-failure count and the remaining cooldown
