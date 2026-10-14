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

The `new_core/loop` orchestration layer SHALL NOT import from `ouroboros`, or from any package
outside the Python standard library and `new_core`. That layer is its nodes, routers, `GraphContext`,
`RunState`, and interface definitions. All access to LLM calls, tool execution, transcript
compaction, context assembly, the owner inbox, and event emission SHALL go through the interfaces.

#### Scenario: The loop layer has no ouroboros dependency

- **WHEN** `new_core.loop` and its submodules are imported
- **THEN** no import resolves to an `ouroboros` module
- **AND** an import-boundary test asserts this

### Requirement: External effects are reached through ABC interfaces held by the context

The graph `GraphContext` SHALL be immutable and SHALL expose the loop's external
dependencies as interfaces, each defined as an abstract base class (`abc.ABC`) with
`@abstractmethod` members: an LLM provider, a tool runner, a transcript compactor, a context
assembler, an owner inbox, and an event sink. Nodes SHALL reach the outside world only through these
interfaces and through an immutable run-configuration value on the context. Concrete implementations
SHALL subclass the interface ABCs.

#### Scenario: A context is built from concrete interface implementations

- **WHEN** concrete adapters subclassing the interface ABCs are supplied together with a run
  configuration
- **THEN** a `GraphContext` is constructed holding those interfaces and configuration
- **AND** nodes invoke external behavior only via those interfaces

#### Scenario: An incomplete interface implementation is rejected

- **WHEN** a class claims to implement an interface but omits an `@abstractmethod` member
- **THEN** it cannot be instantiated

#### Scenario: The context assembler is one of the context's interfaces

- **WHEN** the graph context handed to a node is inspected
- **THEN** it exposes a context-assembler interface alongside the LLM provider, tool runner,
  transcript compactor, owner inbox, and event sink
- **AND** the node that assembles the run's opening transcript reaches it only through that member

### Requirement: The loop runs as a built graph of nodes and routers

The LLM-with-tools round SHALL run as one built graph. A response containing tool calls SHALL route
to `exec-tools`; a response without tool calls SHALL route to `finalize`, which records final content
and reaches the configured terminal. A node reached without the response shape required by that edge
SHALL raise an explicit runtime state error whose behavior is unchanged by Python optimization flags.

#### Scenario: An invalid direct node transition fails explicitly

- **WHEN** `exec-tools` receives no response or no tool calls, or `finalize` receives no response
- **THEN** the node raises a descriptive runtime state error
- **AND** it does not silently continue with partial state

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

The loop graph SHALL preserve equivalent observable behavior for equivalent inputs: the same
final text, accumulated usage, and trace for equivalent inputs; the same ordering of inbox
draining, checkpoint injection, transcript maintenance, model call, and tool execution within
a round; and the same side effects (progress, checkpoint, and usage events; transcript
compaction and cache sealing; tool result appending). The graph is built once per agent and
reused across runs.

#### Scenario: A representative task run yields equivalent output

- **WHEN** a representative sequence of model responses and tool results is driven through the
  migrated loop and through the pre-migration loop with identical inputs
- **THEN** the returned final text, accumulated usage, and trace are equivalent

#### Scenario: Cleanup runs on every exit path

- **WHEN** the run ends — normally, via the round limit, via exhausted recovery, or via an
  exception propagating out of a node
- **THEN** the browser, task mailbox, and temporary tool-artifact cleanups run exactly as
  before

### Requirement: RunConfig and RunState carry a required owner scope

`RunConfig.scope` and `RunState.scope` SHALL be required fields of the engine-level `OwnerScope`
type (`new_core/identity`); neither SHALL be optional and neither SHALL hold `None`. `make_run_state`
SHALL copy `config.scope` into the run state's `scope`, the same way it copies `task_id` and the
active model/effort. Because the identity type lives inside `new_core`, this requirement SHALL be
met without any `new_core/loop` module importing from `ouroboros`.

#### Scenario: make_run_state seeds scope from config

- **WHEN** a run state is built from a run configuration carrying a scope
- **THEN** the state's `scope` is that same `OwnerScope`

#### Scenario: A run config without a scope cannot be built

- **WHEN** a `RunConfig` is constructed without a `scope`
- **THEN** construction raises

#### Scenario: The loop layer stays free of ouroboros imports

- **WHEN** `new_core.loop` and its submodules are imported after scope is threaded in
- **THEN** no import resolves to an `ouroboros` module
- **AND** the existing import-boundary test still passes

### Requirement: The model's own text reaches the owner only while the run's configuration allows it

The node that executes a round's tool calls SHALL publish the model's text beside those calls as a
progress event only while the run configuration permits it, and the decision SHALL be a field of
`RunConfig` rather than a setting the node reads for itself: the loop layer resolves no settings.

Suppressing the event SHALL NOT change what the run records or what the model sees — the text is
still the assistant's turn in the transcript and still in the run's trace — and SHALL NOT extend to
any other emission, which reports the state of the run rather than the model's thinking.

#### Scenario: The narration is published by default

- **WHEN** a round's response carries text beside its tool calls and the configuration permits it
- **THEN** the text is published as a progress event

#### Scenario: The narration is suppressed on request

- **WHEN** the same round runs with the configuration forbidding it
- **THEN** no progress event carries that text

#### Scenario: The transcript and trace are unchanged either way

- **WHEN** the same round runs with the configuration forbidding it
- **THEN** the assistant turn appended to the transcript carries the text
- **AND** the run's trace records it

#### Scenario: Other progress is unaffected

- **WHEN** the configuration forbids the narration and the round falls back to another model
- **THEN** the fallback notice is published as before

### Requirement: A stop fences the outcome of an in-flight model call

The model adapter SHALL check the run's cancellation signal before every attempt and while waiting
between attempts. When a stop is observed while a synchronous model call is in flight, the task body
SHALL stop waiting for it, SHALL consume and discard its late outcome, and SHALL NOT claim that the
underlying request itself was cancelled.

An abandoned main-loop model response SHALL still report its usage once under the original task
and owner scope, without changing the closed run's accumulated usage or publishing response content.

Foreground and detached model calls SHALL observe cancellation while waiting for a model concurrency
slot and after a grant, before provider dispatch. A cancelled waiter SHALL exit without requiring
another call to release its slot. A grant obtained during cancellation SHALL be released without
dispatch, retry, or recording a provider failure.

The graph SHALL evaluate the stop after a model call and before every individual tool execution,
recovery call, language repair, result-processing step, and final result publication. A response that
loses that race SHALL start no tool, retry, fallback model, memory handoff, or owner-facing final
answer. A stop observed while one tool is already executing SHALL prevent every later tool in that
round from starting and SHALL discard the completed tool's result from further loop processing.

Tool-budget acquisition SHALL check cancellation between bounded waits. A stopped waiter SHALL exit
without requiring an existing holder to release its grant; a grant obtained during cancellation
SHALL be released without starting the tool.

#### Scenario: Stop wins against a blocking model response

- **WHEN** a model call is still blocked after the run's cancellation signal is set
- **THEN** the task body reaches its cancelled terminal path without waiting for the response
- **AND** the provider call's later result is discarded

#### Scenario: A stopped model response starts no tool

- **WHEN** a model response containing tool calls returns after a stop was requested
- **THEN** none of those tools execute

#### Scenario: An abandoned main-loop response retains its usage

- **WHEN** a provider returns usage after the stopped main-loop call has returned
- **THEN** one usage event names the original owner, task, and resolved model
- **AND** neither response content nor changes to the closed run's counters are published

#### Scenario: Stop ends a tool-budget wait

- **WHEN** a parallel tool call is waiting for a grant and its task is stopped
- **THEN** the round exits without waiting for the occupied grants to be released
- **AND** no tool body starts and any grant acquired during cancellation is released

#### Scenario: A foreground stop precedes model dispatch

- **WHEN** a foreground run is stopped while waiting for a model concurrency slot
- **THEN** its waiting provider thread exits without sending a provider request
- **AND** if acquisition returns a grant during the stop, that grant is released

#### Scenario: A stop during a tool prevents the rest of the round

- **WHEN** a stop is observed while one tool in a sequential round is already executing
- **THEN** no later tool in that round starts
- **AND** no returned tool result is processed by the loop

#### Scenario: A stop before publication suppresses the answer

- **WHEN** the stop signal is set after a final response was formed but before publication
- **THEN** no owner-facing final answer or memory handoff is published

#### Scenario: Stop wins before sequential tool dispatch

- **WHEN** a stop is observed immediately before a sequential tool call starts
- **THEN** the executor returns without starting the call or raising an execution error

#### Scenario: Stop interrupts context compaction

- **WHEN** a stop is observed during a manual or emergency context-compaction model call
- **THEN** the task stops waiting and discards the late summary without replacing its transcript
- **AND** no subsequent compaction batch or compaction retry starts
- **AND** the compaction model-budget waiter observes the same task cancellation
