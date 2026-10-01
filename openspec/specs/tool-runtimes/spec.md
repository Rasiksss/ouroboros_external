# tool-runtimes Specification

## Purpose

Where a tool body executes. Placement is a named, substitutable object rather than whatever thread
the caller happened to be on: one interface with a single asynchronous operation, two local
implementations — on the calling loop, or on a worker thread — a default carried by every provider,
and a per-descriptor override. The catalog resolves which one runs a call, because it is the only
place holding both the provider and the descriptor. A runtime answers *where* and nothing else:
gates, error text, truncation, and result formatting all have owners above it. Bounding a round —
which of its calls may overlap, how many at once, and how long each may take — stays a caller-level
decision, specified here but never taken by a runtime. One synchronous entry point
bridges callers that are not yet coroutines, and it runs on the calling thread — the bridge is
transitional and shrinks as those callers become asynchronous.

## Requirements

### Requirement: Where a tool body runs is a named object, not the caller's thread

Execution placement SHALL be expressed by a single interface with one asynchronous operation that
receives the owning provider, the tool context, the canonical tool name, and the arguments, and
returns the tool's result text. A runtime SHALL decide only *where* the body runs; it SHALL NOT
apply gates, policy, truncation, or result formatting, all of which run before it.

Two implementations SHALL exist: one that runs the body on the calling coroutine's event loop, and
one that runs it on a worker thread. Adding a further placement SHALL require no change to the
interface or to any provider.

#### Scenario: The catalog delegates placement instead of calling the provider directly

- **WHEN** the catalog executes a name it has resolved to a provider and descriptor
- **THEN** it obtains a runtime and awaits it
- **AND** the provider's own `execute` is reached only through that runtime

#### Scenario: The inline runtime runs the body without leaving the loop

- **WHEN** a tool is executed through the inline runtime
- **THEN** the provider's `execute` is called on the calling coroutine's thread
- **AND** no thread or event loop is created for the call

#### Scenario: The thread runtime runs the body off the loop

- **WHEN** a tool is executed through the thread runtime
- **THEN** the provider's `execute` runs on a worker thread
- **AND** the awaiting coroutine is not blocked while it runs

#### Scenario: A runtime does not reinterpret the result

- **WHEN** a provider's `execute` returns a result or raises
- **THEN** the runtime returns that result, or lets the exception propagate, unchanged
- **AND** the runtime adds no error text, no truncation, and no logging of its own

### Requirement: Every provider declares a default runtime, and a descriptor may override it

Each provider SHALL carry a default runtime, and the provider interface SHALL supply one so that a
provider written without naming it still resolves. A descriptor MAY name a runtime of its own. The
catalog SHALL resolve the runtime for a call as the descriptor's runtime when set, otherwise the
owning provider's default.

The default supplied by the interface SHALL be the thread runtime for as long as every provider's
`execute` is a plain synchronous function. Once a round executes its calls on one event loop, an
inline default would make a batch that policy marks concurrent run strictly one call after another —
silently, with no test failing and nothing but a profile to notice. A provider whose `execute` is
genuinely non-blocking SHALL override it.

#### Scenario: A provider that names no runtime still executes

- **WHEN** a provider is defined without setting a default runtime and one of its tools is executed
- **THEN** the thread runtime is used
- **AND** no attribute error is raised

#### Scenario: No synchronous provider carries the inline default

- **WHEN** the set of providers is enumerated
- **THEN** no provider whose `execute` is a plain synchronous function resolves to the inline runtime

#### Scenario: A descriptor's runtime wins over the provider's default

- **WHEN** a descriptor names a runtime and its owning provider declares a different default
- **THEN** the descriptor's runtime is used

#### Scenario: Resolution follows the same owner as dispatch

- **WHEN** a name resolves to a provider through an alias
- **THEN** the runtime is taken from that provider and that provider's descriptor
- **AND** the provider receives the canonical name

### Requirement: A provider's result may be awaitable

A provider's `execute` SHALL keep its synchronous call shape and MAY return an awaitable instead of
a result. A runtime SHALL await such a result before returning it. This exists so that an
asynchronous source can stop bridging through a thread of its own without changing the provider
interface.

#### Scenario: An awaitable result is awaited by the inline runtime

- **WHEN** a provider's `execute` returns an awaitable and the inline runtime is used
- **THEN** the awaitable is awaited
- **AND** the awaited value is returned as the tool result

#### Scenario: A plain result is returned untouched

- **WHEN** a provider's `execute` returns a result that is not awaitable
- **THEN** it is returned as-is with no coercion beyond the existing string handling

### Requirement: A provider may keep a bound of its own on an awaitable result

A provider whose `execute` returns an awaitable MAY apply its own timeout to that awaitable before
returning it, and that bound SHALL be honoured wherever the awaitable is awaited. A provider SHALL
NOT need a thread or an event loop of its own to apply it.

Every placement SHALL await an awaitable result, so that a provider's return shape does not depend
on where its body was placed.

#### Scenario: An extension tool keeps its per-tool timeout without a thread of its own

- **WHEN** an extension tool with an asynchronous handler and a per-tool timeout shorter than the
  call site's is executed
- **THEN** the per-tool timeout is what bounds it
- **AND** the provider creates no thread and no event loop to apply it

#### Scenario: An awaitable returned from a thread placement is awaited

- **WHEN** a provider running on a thread placement returns an awaitable
- **THEN** the awaited value is the tool result
- **AND** no un-awaited object reaches the caller

### Requirement: A thread placement abandons a body that outlives its deadline

A runtime that places a body on a thread SHALL NOT hold the round open for a body that is still
running when the caller has stopped waiting for it. Completing the round SHALL NOT wait on such a
body, whatever the body eventually does.

A thread placement SHALL propagate the caller's context to the body, so that context-dependent
behaviour — tracing spans, log binding — is the same as it would be inline.

#### Scenario: A tool that ignores its timeout does not delay the round

- **WHEN** a tool body is still running after its timeout has been reported
- **THEN** the round finishes without waiting for that body
- **AND** the body is left to finish on its own

#### Scenario: A hung tool does not starve later tool calls

- **WHEN** one or more tool bodies are abandoned and still running
- **THEN** subsequent tool calls are placed and executed normally
- **AND** they do not wait on the abandoned bodies' capacity

#### Scenario: The caller's context reaches the body

- **WHEN** a context value is set before a tool is executed through a thread placement
- **THEN** the body observes that value
- **AND** it observes the same value it would have observed inline

### Requirement: Synchronous callers reach the asynchronous boundary through one bridge

Tool execution SHALL expose an asynchronous entry point covering a whole round of tool calls, and a
synchronous one with the signature callers use today. The synchronous entry point SHALL be the only
place that crosses from synchronous code into asynchronous execution, it SHALL be reached at most
once per round rather than once per tool call, and it SHALL run the coroutine on the calling thread
rather than scheduling it onto another thread's event loop.

Scheduling onto the process's main event loop is prohibited: tool bodies bridge back to that loop
themselves, and awaiting them from it would deadlock.

No second synchronous entry point SHALL remain below it. Once a round is executed asynchronously,
the per-call synchronous entry point on the catalog SHALL be removed, so that there is exactly one
answer to how synchronous code crosses over.

#### Scenario: A synchronous caller executes a round of tool calls unchanged

- **WHEN** the loop's tool runner executes a round's tool calls
- **THEN** it receives one result per call, in call order, as it did before
- **AND** its own signature and threading are unchanged

#### Scenario: Crossing over happens once per round, not once per call

- **WHEN** a round executes several tool calls
- **THEN** exactly one event loop is created for the whole round
- **AND** no execution path below it creates another

#### Scenario: A tool that bridges to the main loop still completes

- **WHEN** a tool body waits on a coroutine running on the process's main event loop
- **THEN** the call completes
- **AND** the main event loop is not blocked waiting on itself

#### Scenario: The bridge refuses to run inside a running loop

- **WHEN** the synchronous entry point is called from a thread that already has a running event loop
- **THEN** it fails with an error naming the asynchronous entry point as the call to use
- **AND** it does not silently create a second loop

### Requirement: Tool calls in one round run concurrently only where policy already allows it

A round SHALL execute its tool calls sequentially unless every call in the round is eligible for
concurrent execution under existing policy, in which case they SHALL run concurrently. The number
running concurrently SHALL remain bounded, at the bound that applied before execution became
asynchronous.

Eligibility SHALL remain a caller-level decision. A runtime SHALL NOT decide which calls may
overlap.

#### Scenario: An eligible batch overlaps in time

- **WHEN** a round contains several tool calls that policy marks as concurrently executable
- **THEN** their bodies overlap in time
- **AND** each runs on a distinct thread

#### Scenario: One ineligible call makes the whole round sequential

- **WHEN** a round contains a call that policy does not mark as concurrently executable
- **THEN** every call in that round runs one after another

#### Scenario: A batch larger than the bound does not exceed it

- **WHEN** a round contains more eligible calls than the concurrency bound
- **THEN** no more than the bound run at the same time
- **AND** every call still produces a result

### Requirement: A tool that exceeds its soft timeout may still return a real result

A tool call that exceeds its timeout SHALL produce a timeout result and a timeout event. For tools
classified as reviewed mutative, exceeding the timeout SHALL NOT end the call: execution SHALL
continue up to a hard ceiling, a late event SHALL be reported at the soft timeout, and a result
arriving before the ceiling SHALL be returned and marked late.

Reaching the hard ceiling SHALL record a terminal state that states the operation may still complete
later, as it does today.

#### Scenario: A reviewed mutative tool finishing after the soft timeout returns its real result

- **WHEN** a reviewed mutative tool finishes between its soft timeout and the hard ceiling
- **THEN** its real result is returned, marked late
- **AND** a late event was reported when the soft timeout passed

#### Scenario: An ordinary tool exceeding its timeout is not waited on further

- **WHEN** a tool that is not reviewed mutative exceeds its timeout
- **THEN** a timeout result and a timeout event are produced immediately

#### Scenario: An abandoned call that later fails is not reported as an unhandled error

- **WHEN** an abandoned tool body raises after the round has moved on
- **THEN** no unhandled-error diagnostic is emitted for it
