# parallel-runs Specification

## Purpose
How concurrent task execution is bounded — one running task per owner, slot limits, and admission rules.
## Requirements
### Requirement: An instance holds a configured number of runs, and the setting is read

How many tasks an instance runs at once SHALL be configuration, and the executor SHALL read it. A
setting that exists and is read by nothing is a control the operator believes they have.

Each run SHALL hold its own agent, its own stop signal and its own slot. No object carrying a run's
state SHALL be shared between runs, and no lock SHALL be required to make that safe: the safety comes
from not sharing, and a lock retained beside per-run state is where the next shared field is added.

#### Scenario: The configured number of runs is what runs

- **WHEN** the instance is configured for several runs and more tasks are queued than that
- **THEN** that many run at once and the rest wait

#### Scenario: No run-carrying object is shared

- **WHEN** two runs are in flight
- **THEN** each holds its own agent instance
- **AND** neither observes the other's task id, run state, pending events, or usage totals

### Requirement: One owner has at most one running task, enforced where the task is claimed

The one-running-task-per-owner predicate is owned by `agent-runtime`. This spec owns the budget and
admission rules above that predicate.

### Requirement: A run's runtime is measured from when it ran

The elapsed time a timeout is judged against SHALL be measured from when a run began executing, not
from when its record was claimed. A task that waited for a shared resource SHALL NOT be notified or
killed for time it spent waiting.

Measuring from the claim was harmless while a claim meant an immediate start. Once several runs
compete for a bounded resource, it means a task can be killed for another's occupancy having run
nothing at all — a failure that names the wrong task and repeats until the contention clears.

#### Scenario: Waiting is not runtime

- **WHEN** a run waits for a bounded resource before its first round
- **THEN** the wait does not count toward its soft or hard timeout

### Requirement: What runs consume is bounded through the budget seam

Limits on what concurrent runs consume — model calls in flight, tool bodies executing — SHALL be
expressed through the admission-budget interface rather than as process-local primitives, so that a
deployment of several instances can move a limit to a shared backend without a caller changing.

Tool bodies SHALL be bounded for the process and not only within a round. A per-round allowance
multiplied by the number of runs, plus bodies abandoned at their deadline that keep their threads for
a grace period, is not a bound.

A component that is not a run SHALL NOT be able to starve the runs, and SHALL NOT wait without a
deadline on a budget the runs hold. A supervisor loop blocked on a slot held by a run stops dispatching
events, enforcing timeouts and claiming work — the whole instance stalls on a diagnostic call.

The seam SHALL admit an asynchronous caller without blocking the thread it runs on. Acquisition SHALL
therefore be offered in an awaitable form as well as a blocking one, as an abstract member of the
interface implemented explicitly by every implementation rather than as a default that one of them
happens to satisfy. A caller on an event loop that waits through the blocking form stalls every other
piece of work sharing that loop, including work holding nothing and waiting for nothing, and it does
so for as long as the wait lasts however short each individual wait is.

The holder a lease names SHALL identify the work that holds it, not the operating-system thread it
started on. Under an event loop many concurrent holders share one thread, so a thread identifier
names them all alike and the budget cannot tell one holder's release from another's.

Every process that consumes from a budget SHALL compose its limits where it composes its parts. The
seam's fallback limits exist so that an uncomposed process runs rather than fails; they are not
configuration, they cannot be reached by an environment variable, and a process left on them is one
whose ceiling nobody chose. A process intended to run work concurrently SHALL state its own numbers,
so that "not limited" is a number an operator set and not an accident of which root forgot to
initialize.

#### Scenario: The model-call limit is a budget, not a semaphore in a module

- **WHEN** the model client's concurrency limiting is inspected
- **THEN** it acquires through the admission-budget interface
- **AND** no module-level semaphore governs it

#### Scenario: Tool bodies are bounded across runs

- **WHEN** several runs each execute tool calls concurrently
- **THEN** the number of tool bodies executing at once is bounded for the process

#### Scenario: The supervisor loop does not block on a run's budget

- **WHEN** every model-call slot is held by runs and the supervisor makes its own model call
- **THEN** that call gives up on its deadline
- **AND** the loop continues dispatching events, enforcing timeouts and claiming work

#### Scenario: An awaiting caller yields rather than blocking

- **WHEN** every slot of a budget is held and a caller on an event loop awaits one
- **THEN** other work on that loop continues to run while it waits
- **AND** the caller receives its slot when one is released

#### Scenario: Every implementation offers the awaitable form

- **WHEN** the budget interface and its implementations are inspected
- **THEN** the awaitable acquisition is an abstract member of the interface
- **AND** every implementation declares it rather than inheriting a default

#### Scenario: Concurrent holders on one thread are distinguished

- **WHEN** two holders acquire from one budget concurrently on one event loop and one releases
- **THEN** the other still holds its slot

#### Scenario: A process that runs work concurrently composes its limits

- **WHEN** a process that makes model calls finishes composing itself
- **THEN** the budget it uses carries the limits that process composed
- **AND** it is not running on the seam's uncomposed fallback

