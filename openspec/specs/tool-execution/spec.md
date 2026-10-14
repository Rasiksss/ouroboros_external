# tool-execution Specification

## Purpose

How one round's tool-call execution is organized on the ouroboros side. A single executor
class owns the round: its outward senders — the on-disk event journal, the working-memory
invocation recorder, tracing spans, the review-gate outcome sink, and the live-log
`EventSink` — are abstract interfaces injected at construction, and everything between the
public operations (dispatch, timeouts, classification, truncation) is private to the class.
Behavior — the sync bridge, concurrency policy, soft timeout and hard ceiling — is specified
in `tool-runtimes` and is preserved by this organization, not redefined here.

## Requirements

### Requirement: One round's tool work is owned by a single executor object

Tool-call execution and result processing SHALL be operations of one executor class rather than
free module functions. The executor SHALL expose exactly the two-call contract the loop graph
already names: an execution operation returning one raw result per call in call order (with an
asynchronous form and a synchronous bridge form), and a result-processing operation folding
results into the transcript and trace. Every other step — dispatch, timeout handling,
classification, truncation, result-dict construction — SHALL be private to the class.

#### Scenario: The adapter delegates both halves to the executor

- **WHEN** the loop graph's `ToolRunner` adapter handles a round's tool calls and then
  processes their results
- **THEN** both calls are served by one executor instance the adapter was constructed with
- **AND** no module-level execution function is involved

#### Scenario: The adapter is wired, not a wiring site

- **WHEN** the `ToolRunner` adapter is constructed
- **THEN** it takes the executor as a dependency rather than assembling one
- **AND** it reads the run's tool catalog from that executor rather than holding a second
  reference to it

#### Scenario: Per-round state is not retained

- **WHEN** the executor is constructed
- **THEN** it receives only per-task collaborators and sender interfaces
- **AND** the tool context, task id, and active tool names arrive as arguments on the public
  operations, per call

### Requirement: Outward senders are interfaces injected at construction

Every channel the executor reports on SHALL be named as an abstract interface and injected at
construction: the on-disk event journal, the working-memory invocation recorder, the tracing
span factory, the review-gate outcome sink, and the live-log emitter — the existing `EventSink`
interface, of which a run has exactly one. The executor SHALL NOT reach any of these through a
module global, a lazy import, or a path parameter.

#### Scenario: The live-log sink is the run's own

- **WHEN** a task's executor is wired
- **THEN** it is given the same `EventSink` the loop graph carries on its context
- **AND** tool live logs and the graph's own events leave through that one channel

#### Scenario: The journal is reached only through the injected recorder

- **WHEN** the executor records a tool call, a tool error, a policy denial, or a timeout event
- **THEN** it calls the injected journal interface with a stream name, event name, and payload
- **AND** no drive path travels through the executor's signatures

#### Scenario: A test substitutes one sender without faking the rest

- **WHEN** a test constructs the executor with a fake for a single sender interface
- **THEN** the executor operates normally against the other senders' defaults or fakes
- **AND** the fake observes the same payloads production senders would receive

### Requirement: The refactor preserves execution behavior exactly

The executor SHALL preserve the behavior specified in `tool-runtimes` (one sync bridge per
round that refuses to run inside a running loop, policy-gated concurrency with its bound,
soft timeout with abandonment, the reviewed-mutative hard ceiling) and the emitted live-log,
journal, ledger, and trace payloads byte-for-byte.

#### Scenario: Existing behavioral tests pass unchanged in substance

- **WHEN** the tool execution, timeout, parallelism, classification, and policy test suites run
  against the executor
- **THEN** they assert the same statuses, error codes, payload fields, and log lines as before
  the refactor, with only their entry points updated
