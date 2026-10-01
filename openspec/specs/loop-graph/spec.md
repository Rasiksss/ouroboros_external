# loop-graph Specification

## Purpose

The agent's LLM-with-tools loop runs as a built graph over the `new_core` graph engine. Each
semantic phase of a round is a node, branching lives in routers rather than in-node control flow,
and external effects — LLM calls, tool execution, transcript compaction, the owner inbox, event
emission — are reached only through a fixed set of ABC interfaces held by an immutable
`GraphContext`. The `new_core/loop` layer imports nothing from `ouroboros`; concrete wiring lives
in adapters outside it. The engine itself is specified in `graph-core`.

## Requirements

### Requirement: The loop layer imports nothing from ouroboros

The `new_core/loop` orchestration layer — its nodes, routers, `GraphContext`, `RunState`, and
interface definitions — SHALL NOT import from `ouroboros` or from any package outside the Python
standard library and `new_core`. All access to LLM calls, tool execution, transcript
compaction, the owner inbox, and event emission SHALL go through the interfaces.

#### Scenario: The loop layer has no ouroboros dependency

- **WHEN** `new_core.loop` and its submodules are imported
- **THEN** no import resolves to an `ouroboros` module
- **AND** an import-boundary test asserts this

### Requirement: External effects are reached through ABC interfaces held by the context

The graph `GraphContext` SHALL be immutable and SHALL expose the loop's external
dependencies as interfaces, each defined as an abstract base class (`abc.ABC`) with
`@abstractmethod` members: an LLM provider, a tool runner, a transcript compactor, an owner
inbox, and an event sink. Nodes SHALL reach the outside world only through these interfaces and
through an immutable run-configuration value on the context. Concrete implementations SHALL
subclass the interface ABCs.

#### Scenario: A context is built from concrete interface implementations

- **WHEN** concrete adapters subclassing the interface ABCs are supplied together with a run
  configuration
- **THEN** a `GraphContext` is constructed holding those interfaces and configuration
- **AND** nodes invoke external behavior only via those interfaces

#### Scenario: An incomplete interface implementation is rejected

- **WHEN** a class claims to implement an interface but omits an `@abstractmethod` member
- **THEN** it cannot be instantiated

### Requirement: The loop runs as a built graph of nodes and routers

`run_llm_loop`'s per-round phases SHALL be expressed as nodes on the `new_core` graph engine
— guard, apply-overrides, drain-inbox, checkpoint, prepare-transcript, llm-call, recover,
exec-tools, finalize, and round-limit-final — with branching expressed as routers rather than
in-node control flow. The graph SHALL be built (validated) once and then invoked. Nodes SHALL
NOT name their own successors.

#### Scenario: A tool-calling response routes to tool execution

- **WHEN** the llm-call node produces a response containing tool calls
- **THEN** the router after llm-call selects the exec-tools node
- **AND** after tools execute, control returns to the guard node for the next round

#### Scenario: A plain-text response routes to finalize and ends the run

- **WHEN** the llm-call node produces a response with no tool calls and finalize injects no
  further follow-up
- **THEN** the run reaches `END` with the response text recorded as the final content

### Requirement: The round limit is a domain edge, distinct from the engine step budget

The `MAX_ROUNDS` limit SHALL be enforced by the guard router, not by the engine's `max_steps`
cap. When the round index exceeds `MAX_ROUNDS`, the run SHALL route to a terminal node that
injects the round-limit notice and performs one final model call, then reaches `END`. The
engine's `max_steps` SHALL remain a separate anti-infinite-loop backstop.

#### Scenario: Exceeding MAX_ROUNDS produces one final wrap-up call

- **WHEN** the round index exceeds the configured `MAX_ROUNDS`
- **THEN** the guard router routes to the round-limit-final node
- **AND** that node injects the round-limit notice, performs a single final model call, and
  the run reaches `END`

### Requirement: A cooperative stop signal is a domain edge on the guard router

`GraphContext` SHALL carry a `CancellationSignal`. It SHALL have no default: every caller that builds
a run names the signal that ends it. The guard router SHALL evaluate it before the round-limit
comparison and, when it reports a stop, SHALL route to the configured terminal without starting
another round.

The stop SHALL NOT route to a wrap-up node and SHALL NOT trigger a further model call: unlike the
round limit, a stop means no further answer is wanted. The run SHALL reach the terminal with no final
content recorded, which is what tells the emission side that there is nothing to publish.

The signal SHALL be an interface in `new_core/loop/interfaces.py` beside the other loop interfaces —
`is_stopped()` to observe a stop and `request_stop()` to ask for one — and never a plain callable on
the frozen run configuration: what implements it ranges from a process-local event to shared state
read across supervisor instances, and the loop layer keeps importing nothing from `ouroboros` either
way. No never-stopping implementation SHALL exist in production code; a run that cannot be stopped is
a test fixture, not a shipped default.

#### Scenario: A stopped run does not start another round

- **WHEN** the guard router runs and the configured signal reports a stop
- **THEN** the run routes to the configured terminal
- **AND** no further model call is made
- **AND** no final content is recorded on the run state

#### Scenario: The stop check precedes the round-limit check

- **WHEN** both the stop signal is set and the round index exceeds `MAX_ROUNDS`
- **THEN** the run routes to the terminal without the round-limit wrap-up call

#### Scenario: A run whose signal reports no stop is unchanged

- **WHEN** the context's cancellation signal reports no stop
- **THEN** the guard router behaves exactly as it did before, checking only the round limit

#### Scenario: The loop layer stays free of ouroboros imports

- **WHEN** the stop signal is threaded into the loop layer
- **THEN** it is carried as a `new_core/loop` interface on `GraphContext`
- **AND** no module under `new_core/loop` imports from `ouroboros`

### Requirement: Model, effort, and use-local overrides are applied at the top of the round

Overrides that tools set during execution SHALL be drained from the tool runner after tool
execution into run state, and applied to the active model, effort, and use-local at the start
of the following round. Draining SHALL reset the override channel so an override applies once.

The same channel SHALL carry requests to add tools to the prompt surface. Each request SHALL carry
the tool name and its schema, the schema staying opaque to the loop layer. The apply-overrides node
SHALL append requested schemas to the run state's tool schemas and add their names to the active
tool names, skipping names already active.

#### Scenario: A tool-set model override takes effect next round

- **WHEN** a tool sets a model override during exec-tools in round N
- **THEN** the exec-tools node drains it into run state
- **AND** the apply-overrides node updates the active model at the start of round N+1
- **AND** the override does not re-apply in later rounds

#### Scenario: A tool-requested tool becomes active next round

- **WHEN** a tool requests enabling another tool during exec-tools in round N
- **THEN** the exec-tools node drains the request into run state
- **AND** the apply-overrides node adds its schema and name to the active surface at the start of
  round N+1
- **AND** the round N+1 model request carries that tool's schema
- **AND** the request does not re-apply in later rounds

#### Scenario: Enabling an already active tool changes nothing

- **WHEN** a drained request names a tool already in the active tool names
- **THEN** the apply-overrides node leaves the tool schemas unchanged

### Requirement: Empty-response recovery is attempted at most once

When a model call returns no response, the recover node SHALL attempt recovery — emergency
compaction with a same-model retry, then a single fallback-model call if configured — guarded
so recovery is attempted at most once per round and does not loop back into the primary
llm-call node. If recovery still yields no response, the run SHALL end with a descriptive
failure message as the final content.

#### Scenario: Fallback model recovers an empty primary response

- **WHEN** the primary model returns no response and a distinct fallback model is configured
- **THEN** the recover node retries via the fallback model and, on success, routes to
  exec-tools or finalize per the response
- **AND** the primary llm-call node is not re-entered as part of recovery

#### Scenario: Exhausted recovery ends with a failure message

- **WHEN** both the primary retry and the fallback call return no response
- **THEN** the run reaches `END` with a descriptive failure message as the final content

### Requirement: Observable loop behavior is preserved

Migrating the loop to the graph SHALL preserve `run_llm_loop`'s observable behavior: the same
final text, accumulated usage, and trace for equivalent inputs; the same ordering of inbox
draining, checkpoint injection, transcript maintenance, model call, and tool execution within
a round; and the same side effects (progress, checkpoint, and usage events; transcript
compaction and cache sealing; tool result appending). The public `run_llm_loop` return contract
SHALL be unchanged; it MAY accept an optional prebuilt graph so callers assemble the graph once
and reuse it, rather than reassembling it on every run.

#### Scenario: A representative task run yields equivalent output

- **WHEN** a representative sequence of model responses and tool results is driven through the
  migrated loop and through the pre-migration loop with identical inputs
- **THEN** the returned final text, accumulated usage, and trace are equivalent

#### Scenario: Cleanup runs on every exit path

- **WHEN** the run ends — normally, via the round limit, via exhausted recovery, or via an
  exception propagating out of a node
- **THEN** the browser, task mailbox, and temporary tool-artifact cleanups run exactly as
  before
