# working-retrieval Specification

## Purpose

How the agent retrieves its own past work while a task is running: episodes of completed tasks are
filed and read under the four axes of the scope they ran under, episodes are only written for tasks
with a task id, enrichment reads are bounded by their task, tool invocations are read by task while
carrying ownership for the record, and retrieval failure is never silent.

## Requirements

### Requirement: Episodes are filed and read under the same four axes

A completed task's episode SHALL be stored under the four axes of the scope the task ran under,
and the retrieval read SHALL select episodes by all four of those axes. The write and the read
SHALL derive the axes from the same scope value rather than each re-deriving identity from what is
at hand.

#### Scenario: An episode is retrievable in the chat it was filed in

- **WHEN** a task completes under a scope and a later continuity query runs under the same scope
- **THEN** that episode is a retrieval candidate

#### Scenario: An episode of another chat is not a candidate

- **WHEN** two episodes carry identical search terms and differ only in `chat_id`
- **AND** a continuity query runs under the scope of the first
- **THEN** the retrieval section contains the first episode and not the second

#### Scenario: An episode of another surface is not a candidate

- **WHEN** two episodes carry identical search terms and differ only in `surface_id`
- **AND** a continuity query runs under the scope of the first
- **THEN** the retrieval section contains the first episode and not the second

### Requirement: An episode is not written without a task

An episode SHALL be built only for a task that has a task id. Work with no task id SHALL NOT
produce an episode row.

#### Scenario: No task id yields no episode

- **WHEN** episode construction is attempted for work whose task id is empty
- **THEN** no episode is built and no row is written

### Requirement: The enrichment read is bounded by its task and can never be unfiltered

The read that enriches an episode with the tool invocations of its own task SHALL require a task
id. There SHALL be no value of that parameter — empty string, `None`, or otherwise — for which the
read returns rows belonging to other tasks.

#### Scenario: An empty task id is not a wildcard

- **WHEN** the invocation read surface is called without a usable task id
- **THEN** it does not return rows of other tasks

#### Scenario: Enrichment stays within the task

- **WHEN** invocations of several tasks and several scopes are stored and one episode is enriched
- **THEN** the merged references, entities and search terms come only from invocations of that
  episode's task

### Requirement: Tool invocations are read by their task, and carry ownership for the record

The tool-invocation read SHALL select by task id. The ownership axes SHALL be stored on each
invocation row, and SHALL NOT be part of that read's predicate, so that an invocation recorded
under one scope remains reachable through its task exactly as it is today.

#### Scenario: An invocation is found through its task regardless of axes

- **WHEN** an invocation is recorded under one scope and an episode of the same task is enriched
- **THEN** the invocation is merged into that episode

### Requirement: Retrieval failure is not silent

A retrieval read that returns nothing because its scope could not be assembled SHALL be
distinguishable from one that returns nothing because no episode matched. Failing to build the
section or to file an episode SHALL NOT fail the task, but the guard that keeps it from doing so
SHALL NOT also absorb a programming error, and what it does absorb SHALL be reported above debug
level.

#### Scenario: A malformed scope surfaces instead of emptying the section

- **WHEN** the retrieval section is built with a scope that cannot be assembled
- **THEN** the failure surfaces rather than being returned as an empty section indistinguishable
  from "nothing matched"

#### Scenario: A store failure is reported but does not fail the task

- **WHEN** the store raises while an episode is being filed
- **THEN** the task still completes
- **AND** the failure is reported above debug level
