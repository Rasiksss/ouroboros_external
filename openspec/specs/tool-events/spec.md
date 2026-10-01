# tool-events Specification

## Purpose

How a running tool reports outward. A tool names the event, not the transport: `ToolContext` carries
a `ToolEvents` interface declaring one method per kind of outward event — token usage from a
model call the tool made itself, a best-effort live log line, and a named lifecycle event. The
concrete implementation is built where the task's `ToolContext` is assembled, binds the task
identity once, and owns every fallback for the supervisor queue being absent, so no call site
branches on whether a transport exists. The queue's wire format and the supervisor's handler table
are outside this capability; the tool-facing contract is `ToolContextProtocol`, which declares the
channel.

## Requirements

### Requirement: Tools report outward through an interface, not a transport

`ToolContext` SHALL expose the outward event channel as an abstract interface. No field on
`ToolContext` SHALL be a queue, and no method on the interface SHALL name or expose one. A tool
SHALL reach the channel only through its context; it SHALL NOT construct an implementation, read a
module global, or import the supervisor to reach the queue.

#### Scenario: A tool emits without knowing the transport

- **WHEN** a tool reports usage, a log line, or a lifecycle event
- **THEN** it calls a method on the events interface held by its tool context
- **AND** it passes no queue, no event-type string of the supervisor's wire format for usage or log,
  and no fallback of its own

#### Scenario: No production tool reaches a queue

- **WHEN** the tool modules are inspected for transport access
- **THEN** none of them reference a queue object, `put`, or `put_nowait`

### Requirement: The interface carries exactly the kinds of event tools emit

The interface SHALL declare one method per kind of outward event a tool produces: token usage for a
model call made outside the main loop, a best-effort live log line, and a named lifecycle event.
Each method SHALL take only what varies per call; anything constant for the task SHALL NOT be a
parameter.

#### Scenario: Usage from a tool-initiated model call is accounted

- **WHEN** a tool calls a model directly and receives a usage report
- **THEN** it emits usage naming the model and the usage report, plus the category and source that
  classify the call
- **AND** the resulting event carries the same fields as usage emitted by the main loop, including
  provider, model category, api key type, source, and timestamp

#### Scenario: A lifecycle event is named by the tool

- **WHEN** a tool reports the end of a skill execution, successfully or not
- **THEN** it emits a lifecycle event whose type it names and whose payload it supplies
- **AND** the event reaches the supervisor handler registered for that type

### Requirement: Task identity is bound when the channel is built

The implementation SHALL be constructed with the identity of the task it reports for, and SHALL
stamp that identity onto every event it emits. A caller SHALL NOT pass the task id per call, and
SHALL NOT be able to emit an event attributed to a different task.

#### Scenario: Every emitted event carries the task it came from

- **WHEN** any tool of a running task emits an event
- **THEN** the event carries that task's id without the tool having supplied it

### Requirement: An absent transport is handled by the implementation

When no supervisor queue is available, the implementation SHALL apply the fallback appropriate to
the kind of event rather than dropping it silently or raising: usage SHALL be accounted locally,
lifecycle events SHALL be recorded to the event stream on disk, and log lines MAY be discarded.
Callers SHALL NOT branch on whether a transport exists.

#### Scenario: Usage is still accounted without a supervisor

- **WHEN** usage is emitted from a context built with no queue
- **THEN** the usage is added to the local usage counters
- **AND** the caller performs no check and takes no alternative path

#### Scenario: A lifecycle event survives a missing queue

- **WHEN** a lifecycle event is emitted from a context built with no queue
- **THEN** it is written to the on-disk event stream under its own type

#### Scenario: A queue failure does not fail the tool

- **WHEN** publishing to the queue raises
- **THEN** the failure is logged at debug level and the tool call returns its normal result

### Requirement: The channel is assembled with the rest of the tool context

The events implementation SHALL be built where the task's `ToolContext` is assembled, from the
queue, drive root, and task identity already available there. No graph node SHALL assign the
channel onto an already-built context. A `ToolContext` constructed without one SHALL still be
usable, defaulting to an implementation that emits nothing.

#### Scenario: The context arrives at the tool-binding node complete

- **WHEN** the node that binds the tool surface runs
- **THEN** the context it receives already carries its events channel
- **AND** the node assigns nothing onto the context for the purpose of emitting events

#### Scenario: A directly constructed context is safe to use

- **WHEN** a `ToolContext` is constructed without an events channel and a tool emits through it
- **THEN** the call succeeds and produces no event

### Requirement: The tool ABI declares the channel

The minimum tool-context contract SHALL declare the events channel alongside the other fields tools
depend on, so that a tool written against the contract can rely on it.

#### Scenario: The contract names the channel

- **WHEN** the tool-context protocol is checked against a context that carries an events channel
- **THEN** the protocol declares the channel and the check passes
