# schedule-addressing Specification

## Purpose

Which conversation a schedule belongs to, and what that decides about reaching it and about firing
it. A schedule is created inside one conversation and carries it as an axis of its identity beside
the owner and the agent, so every statement a tool reaches is keyed on all three and a schedule is
neither visible nor mutable from anywhere else. On a surface where one owner covers a whole
deployment the conversation is the only axis separating two people, which is why the isolation lives
in the predicate rather than in a check above it, and in a stored column rather than in the
encrypted payload that carries the delivery scope. The dispatching tick is deliberately not narrowed
that way: it has no caller, so it owns every owner's rows and every conversation's alike. Two
conditions refuse a slot that has already been claimed — a scope naming no conversation the shared
chat id policy calls real, and a conversation record that exists and says the owner retired it — and
both are permanent, so the row leaves the due query and waits for the owner instead of storing and
paying for an answer behind a conversation nobody can read.

## Requirements
### Requirement: A schedule belongs to the conversation it was created in

A schedule SHALL carry the conversation it was created in as an axis of its identity, beside the
owner and the agent. Every statement a tool reaches SHALL be keyed on all three, and the isolation
SHALL be in the predicate rather than in a check above it. This covers reading one schedule,
listing a page of them, counting them by state, and replacing one.

The dispatcher's statements SHALL NOT be narrowed this way. A tick has no caller, so it owns every
owner's rows, every agent's and every conversation's alike, and a due scan that skipped a
conversation would leave its rows selected forever with no outcome written.

#### Scenario: A schedule is invisible from another conversation

- **WHEN** an owner creates a schedule in one conversation and lists schedules in another
- **THEN** the listing does not contain it
- **AND** the counts by state do not count it

#### Scenario: A schedule cannot be changed from another conversation

- **WHEN** a caller in one conversation names the id of a schedule created in another
- **THEN** cancelling, editing, confirming, pausing and resuming each answer that no such schedule exists
- **AND** the stored schedule is unchanged

#### Scenario: Two peers of one deployment do not share a listing

- **GIVEN** a surface on which every conversation resolves to the same owner and agent
- **WHEN** two participants each hold a schedule
- **THEN** neither participant's listing contains the other's schedule, its text or its chat id

#### Scenario: The tick is not narrowed by conversation

- **WHEN** due slots exist in several owners' conversations at once
- **THEN** one dispatch pass may claim slots from all of them
- **AND** every slot it claims is delivered under the scope stored on its own row

### Requirement: The conversation is a stored column, never a read of the payload

The conversation SHALL be a column on the schedule row, written when the row is created from the
same scope the delivery is stored under, so the two cannot disagree. It SHALL NOT be recovered from
the stored payload at query time: the payload is encrypted at rest wherever field encryption is on,
so no statement can filter on it, and a filter applied after the read would shorten a page instead
of narrowing it — the rows a page loses would be crowded out of every later page too.

A listing's cursor SHALL count the rows the statement selected, so paging through one conversation's
schedules reaches all of them.

#### Scenario: Paging stays inside the conversation

- **WHEN** an owner holds more schedules in several conversations than one page carries
- **AND** the owner pages through one conversation's schedules to the end
- **THEN** every page holds only that conversation's schedules
- **AND** every one of that conversation's schedules is reached

### Requirement: Delivery moves within a conversation, never into it from another

Moving a schedule's delivery SHALL name a schedule of the conversation the caller is in, and SHALL
re-anchor that schedule's delivery hint to the caller now asking. Where a conversation has several
participants this is what directs a later run to the one who asked rather than to whoever asked
first. A schedule of another conversation SHALL be refused as absent, exactly as every other
schedule of another conversation is.

#### Scenario: A group schedule is re-anchored to the participant asking now

- **GIVEN** a schedule created by one participant of a shared conversation
- **WHEN** another participant of that conversation moves it
- **THEN** the stored delivery names the participant who asked, in the same conversation
- **AND** later runs reach that participant

#### Scenario: A schedule of another conversation cannot be moved

- **WHEN** a caller names the id of a schedule created in another conversation
- **THEN** the move answers that no such schedule exists
- **AND** the stored delivery is unchanged

### Requirement: A due slot is refused when its conversation has been retired

Before delivering a claimed slot the system SHALL consult the conversation record for the scope the
schedule stores. A record that exists and is not active SHALL refuse the slot: no message is
published and no task is submitted, the receipt records the failure as a property of the row, the
schedule leaves the due query, and its state says it needs attention. Firing into a retired
conversation would store and pay for an answer behind a conversation the owner can neither write to
nor see.

A scope with no record SHALL be delivered. The conversation record covers one surface, so every
other surface holds conversations that have no record and never will, and reading absence as
retirement would stop their schedules.

Restoring the conversation SHALL make the schedule reachable again, and resuming it SHALL be the
owner's action rather than an automatic consequence of the restoration.

#### Scenario: A retired conversation stops its schedules

- **GIVEN** an active recurring schedule in a conversation the owner has retired
- **WHEN** its slot comes due and is claimed
- **THEN** nothing is delivered to the conversation
- **AND** the receipt records a failure that is permanent
- **AND** the schedule needs attention and has no next run

#### Scenario: A conversation with no record still fires

- **GIVEN** a schedule whose conversation the registry holds no record of
- **WHEN** its slot comes due
- **THEN** it is delivered under the scope stored on its row

#### Scenario: Restoring the conversation restores the schedule to view

- **GIVEN** a schedule stopped because its conversation was retired
- **WHEN** the owner restores the conversation and lists schedules there
- **THEN** the schedule is listed as needing attention
- **AND** the owner can resume it

### Requirement: A deliverable conversation is one the chat id policy calls real

A schedule SHALL be refused a delivery conversation only when the scope names no conversation, or
names one the shared chat id policy calls synthetic. No other value SHALL be refused. The policy is
the single authority on which ids are synthetic, and a second, wider rule stated at the scheduler
would refuse conversations every other part of the system serves.

#### Scenario: An ordinary conversation whose id reads as zero is served

- **WHEN** a caller creates, lists or cancels a schedule in a conversation whose id is `0`
- **THEN** the operation is served like any other conversation's

#### Scenario: A synthetic conversation is refused

- **WHEN** a schedule operation is attempted from an agent-to-agent or skill-bridge conversation
- **THEN** it is refused as having no usable delivery conversation

### Requirement: Existing schedules take their conversation from the delivery they already stored

The revision that introduces the column SHALL fill it from each row's stored delivery scope, so a
schedule created before the column keeps working in the conversation it has always delivered to. The
read SHALL complete before the schema is altered.

A stored payload that cannot be decrypted SHALL abort the revision against an untouched schema: the
key belongs to the process, not to the row, and a process without it would otherwise strip every
schedule in the deployment of its conversation. A payload that decrypts but cannot be parsed, or
that names no delivery scope, SHALL be parked — no conversation, no next run, needing attention —
and the number of parked rows SHALL be reported, because such a row could never have fired.

#### Scenario: A schedule created before the column keeps its conversation

- **GIVEN** a schedule stored before the conversation was a column
- **WHEN** the revision is applied
- **THEN** its conversation is the one its stored delivery scope names
- **AND** it is listed in that conversation and nowhere else

#### Scenario: A process that cannot decrypt refuses the upgrade

- **GIVEN** a process without the field key for an encrypted deployment
- **WHEN** the revision is applied
- **THEN** it fails
- **AND** the schema is unchanged

#### Scenario: A payload that names no delivery scope is parked

- **GIVEN** a stored schedule whose payload decrypts but names no delivery scope
- **WHEN** the revision is applied
- **THEN** the row has no conversation, no next run and needs attention
- **AND** the count of such rows is reported
