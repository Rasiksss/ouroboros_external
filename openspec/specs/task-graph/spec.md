# task-graph Specification

## Purpose

The agent's whole per-task lifecycle — context assembly, the LLM-with-tools loop, and result
emission — runs as a single built graph over the `new_core` engine, assembled once and invoked once
per task. Context assembly and tool binding are nodes at the head of the graph; result emission is
an effects-interface capability any node can invoke rather than a dedicated terminal stage; and
`Agent.handle_task` collapses to the lifecycle scaffolding that cannot be a node wrapped around one
`invoke`. The graph reuses the `loop-graph` nodes unchanged and is authored in `new_core/loop`
with every one of its nodes, each reaching outside `new_core` only through an interface, so the
`new_core/loop` layer stays free of `ouroboros` imports. The loop itself is specified in
`loop-graph`; the engine in `graph-core`.
## Requirements
### Requirement: The whole task lifecycle runs as one built graph

The agent's per-task work — context assembly, the LLM-with-tools loop, and result emission —
SHALL be expressed as a single graph over the `new_core` engine, built once per agent and invoked
once per task. `Agent.handle_task` SHALL NOT contain the lifecycle *logic* itself; it SHALL reduce to
the scaffolding that cannot be a node (trace span, heartbeat, temporary workspace, and the `finally`
cleanup) wrapped around a single `invoke`. The graph SHALL NOT be reassembled between invocations of
the agent that owns it.

Where an agent is constructed per run, the graph is therefore built per run with it. That is not a
weakening of the rule but its consequence: the rule exists so that a graph is not rebuilt inside the
task path, and assembly is an in-memory operation whose cost was measured as negligible beside a run.
What the rule forbids is a graph reassembled per invocation, not an agent that lives for one task.

#### Scenario: A task runs by invoking a prebuilt task graph

- **WHEN** the agent handles a task
- **THEN** it invokes a prebuilt task graph rather than executing context-assembly, loop, and
  result-emission logic inline
- **AND** the graph is not reassembled during the task

#### Scenario: A per-run agent brings its own graph

- **WHEN** two runs are in flight
- **THEN** each invokes the graph built with its own agent
- **AND** neither reassembles a graph inside the task path

#### Scenario: handle_task holds only lifecycle scaffolding around the invoke

- **WHEN** `handle_task` runs
- **THEN** the only work it performs outside `invoke` is the trace span, heartbeat, temporary
  workspace lifecycle, and the `finally` cleanup
- **AND** context assembly and result emission happen inside graph nodes

### Requirement: Task-start bookkeeping is a graph node

A `record_task_started` node at the head of the task graph SHALL open the task's record before any
context is assembled: the durable task-received event and the running-status write. That node SHALL
reach both through the effects interface's `task_started` member, mirroring `emit_result` at the
terminal end of the run, and SHALL NOT perform any prompt assembly. It SHALL signal typing before
opening the record, so the order of emitted events is unchanged from when one node held both jobs.

The task's lineage — its parent and root task ids — SHALL be resolved exactly once per run and
handed to both the effects interface and the context assembler, rather than derived independently by
each.

#### Scenario: The task record is opened before context assembly

- **WHEN** the task graph runs from `START`
- **THEN** the `record_task_started` node emits the task-received event and writes the running status
- **AND** it does so before `prepare_context` builds the tool context

#### Scenario: Typing is signalled before the record is opened

- **WHEN** `record_task_started` runs
- **THEN** the typing signal is emitted first and the task record is opened second
- **AND** the resulting event order is the same as before task-start bookkeeping and context
  assembly were separate nodes

#### Scenario: Lineage is resolved once per run

- **WHEN** the parent and root task ids are needed both for the task record and for the built
  `ToolContext`
- **THEN** they are resolved by a single call while the run is being built
- **AND** neither the effects interface nor the context assembler re-reads the task record to
  derive them again

#### Scenario: An incomplete effects implementation is rejected

- **WHEN** a class claims to implement the effects interface but omits the `task_started`
  `@abstractmethod`
- **THEN** it cannot be instantiated

### Requirement: Context assembly is a graph node

A `prepare_context` node at the head of the task graph SHALL build the task's `ToolContext`, bound to
the run's transcript list, and SHALL write that context into `RunState` and nothing else onto it. An
`open_transcript` node SHALL recall relevant memories and assemble a fresh run's initial transcript via
`build_llm_messages`, filling that same list in place. Context assembly SHALL NOT touch the tool
registry and SHALL NOT open the task's record. `prepare_context` SHALL read `state.scope` and pass it
into context assembly, so the built `ToolContext` carries that exact scope and context assembly
derives no identity from the task dict itself.

Both nodes SHALL live in `new_core/loop` and SHALL reach assembly through a `ContextAssembler`
interface on the graph context rather than through a closure over the agent. The ouroboros-side
implementation of that interface SHALL hold the assembly policy — the tool context, the soft-cap
budget, memory recall, and the prompt build.

#### Scenario: The head populates run state before the loop

- **WHEN** the task graph runs a task that was never parked
- **THEN** `prepare_context` builds the tool context and `open_transcript` recalls memories and builds
  the initial messages into `RunState`
- **AND** neither installs that context on the registry nor derives tool schemas
- **AND** neither emits the task-received event nor writes the running status

#### Scenario: The tool context carries the run state's scope

- **WHEN** `prepare_context` builds the task's `ToolContext`
- **THEN** the context's `scope` is the same `OwnerScope` object held by `RunState.scope`
- **AND** no identity field is read from the task dict for it

#### Scenario: Context assembly is reached through an interface

- **WHEN** the `prepare_context` and `open_transcript` nodes are inspected
- **THEN** their only external reach is the graph context's `ContextAssembler` member
- **AND** they hold no closure over the agent, and the module they live in imports nothing from
  `ouroboros`

#### Scenario: An incomplete context-assembler implementation is rejected

- **WHEN** a class claims to implement the context-assembler interface but omits either of its
  `@abstractmethod`s
- **THEN** it cannot be instantiated

### Requirement: Tool binding is a separate graph node

A `prepare_tools` node placed between `open_transcript` and the loop SHALL bind tools to the
prepared task: deriving the initial tool schemas for this task's `ToolContext`, and attaching the
tool surface to that context.

The node SHALL reach that work through the `ToolRunner` interface on the graph context. It SHALL
NOT read a tool catalog from the graph context, and SHALL NOT reach one through a closure over the
agent. Which catalog serves the run SHALL be an implementation detail of the adapter behind that
interface, and the graph context SHALL carry no concrete tool catalog.

The node SHALL write the tool schemas, the active tool names, and the seeded pending overrides into
`RunState`. Loop nodes SHALL run only after `prepare_tools` has populated that state, or, for a
resumed run, after `resume` has restored it. The node SHALL
NOT install context on, or publish, a shared tool registry, and SHALL NOT replace tool handlers.

Configured tool names SHALL select a repo-defined, `type: tool` skill, or `type: extension` tool by
its canonical name, and a skill tool also by its stable `<skill>.<tool>` name when no other tool
shares that name; a selected tool
enters the first request under its canonical name. An extension tool SHALL enter the first request
only when it is configured. The extension schema budget SHALL be spent only on configured extension
tools, admitting them in configured order; a configured extension tool whose schema does not fit the
remaining budget SHALL be left out of the first request and SHALL remain discoverable.

#### Scenario: prepare_tools builds the surface before the loop

- **WHEN** the task graph reaches `prepare_tools`
- **THEN** the initial tool schemas are derived from this task's `ToolContext`, not a previous task's
- **AND** the tool schemas and active tool names are available in `RunState` when the first loop
  node executes

#### Scenario: Initial surface selection is one decision behind one interface

- **WHEN** the starting set is selected for a run
- **THEN** the task constraint, attachments, configured tool names, and the extension schema budget
  are all applied by the single implementation the node reaches through `ToolRunner.bind_surface`
- **AND** no provider decides which of its tools belong in the first request

#### Scenario: The graph context carries no tool catalog

- **WHEN** the graph context handed to a node is inspected
- **THEN** it exposes the loop interfaces and the run configuration, and no concrete tool catalog
- **AND** `prepare_tools` still forms the starting surface, through the interface rather than around it

#### Scenario: A configured type: tool skill tool enters the first request

- **WHEN** the configured tool names list an eligible `type: tool` skill tool as `<skill>.<tool>`
- **THEN** its schema is in the first request under its canonical name

#### Scenario: A stable name two tools share selects neither

- **WHEN** the configured tool names list a `<skill>.<tool>` name that more than one tool carries
- **THEN** that name selects none of them
- **AND** each of them is still selected by its canonical name

#### Scenario: An unconfigured extension tool stays out of the first request

- **WHEN** a live extension tool is not among the configured tool names
- **THEN** its schema is absent from the first request
- **AND** it remains discoverable for `enable_tools`

#### Scenario: A configured extension tool enters the first request

- **WHEN** the configured tool names list a live extension tool as `<skill>.<tool>` or by its
  provider-safe name
- **THEN** its schema enters the first request under the provider-safe name

#### Scenario: The extension schema budget admits configured tools in configured order

- **WHEN** two configured extension tools each fit the extension schema budget alone but not together
- **THEN** the one configured first enters the first request and the other is left out
- **AND** the one left out remains discoverable

### Requirement: Result emission is an interface capability any node can invoke

Final task-result publication SHALL be reachable from any node through an effects interface on the
graph context, not confined to a dedicated terminal pipeline stage. The effects interface (the
loop's `EventSink`) SHALL be extended with an `emit_result` member covering final-result
publication and the failed-result write; nodes that produce a terminal outcome — normal finish,
round limit, or error — SHALL publish it through that member. Concrete implementations SHALL
subclass the interface ABC.

#### Scenario: A finalizing node publishes its result through the interface

- **WHEN** a node produces the task's terminal outcome (final answer, round-limit wrap-up, or error)
- **THEN** it publishes the result by calling the effects interface's `emit_result` member
- **AND** it does not depend on a separate downstream emit node to publish

#### Scenario: An incomplete effects implementation is rejected

- **WHEN** a class claims to implement the effects interface but omits the `emit_result`
  `@abstractmethod`
- **THEN** it cannot be instantiated

### Requirement: Result emission consults the task record before publishing to the owner

The effects interface's `emit_result` SHALL decide owner-facing publication from the task record's
current status together with the presence of a final answer: an active record publishes as before, a
terminal record with an answer publishes it prefixed with a late-result marker, and a terminal record
with no answer publishes nothing to the owner.

End-of-task bookkeeping that is not owner-facing — usage accounting, the task episode, metrics, and
the `task_done` event — SHALL continue in every case, so a stopped or late-finishing task is still
accounted for.

#### Scenario: A terminal record with an answer gets the marker

- **WHEN** `emit_result` runs for a task whose record is already terminal and a final answer exists
- **THEN** the owner message carries the late-result marker ahead of the answer

#### Scenario: A terminal record with no answer publishes nothing to the owner

- **WHEN** `emit_result` runs for a task whose record is already terminal and no final answer exists
- **THEN** no owner message is emitted
- **AND** usage, episode, metrics, and `task_done` are still recorded

#### Scenario: An active record publishes unchanged

- **WHEN** `emit_result` runs for a task whose record is still active
- **THEN** the owner message is built exactly as before, with no marker

### Requirement: The loop layer stays pure and authors the whole task graph

The `new_core/loop` layer — its nodes, routers, context, state, interfaces, and graph builders —
SHALL continue to import nothing from `ouroboros`.

Every task-graph node SHALL live in `new_core/loop` and reach outside `new_core` only through a loop
interface on the graph context. `record_task_started`, `prepare_context`, `resume`, `open_transcript`,
`prepare_tools` and `emit` are the task phases; `finalize` belongs to the round the standalone loop and the task
graph share. The full task graph SHALL be assembled in `new_core/loop/build.py`, and nothing under
`ouroboros/agent/` SHALL register a node. New interfaces SHALL be introduced only where a node
genuinely reaches outside `new_core`; moving a node down into `new_core/loop` SHALL follow the
introduction of such an interface rather than precede it.

#### Scenario: The loop layer has no ouroboros dependency after the migration

- **WHEN** `new_core.loop` and its submodules are imported
- **THEN** no import resolves to an `ouroboros` module
- **AND** the loop nodes reused by the task graph are unchanged

#### Scenario: The task graph is authored in the loop layer

- **WHEN** the task graph is built
- **THEN** every node, `emit` included, comes from `new_core/loop`
- **AND** the assembly itself happens in `new_core/loop/build.py`

#### Scenario: A node moves down only behind an interface

- **WHEN** a task-phase node stops importing `ouroboros` and moves into `new_core/loop`
- **THEN** every external effect it had reaches the outside through a loop interface on the graph
  context
- **AND** the ouroboros-side implementation of that interface holds the policy the node used to hold

### Requirement: Observable task behavior is preserved

The task graph SHALL preserve `handle_task`'s observable behavior: for
equivalent inputs, the same final task result, accumulated usage, and trace; the same emitted
events and side effects (task-received / running / result records, progress, checkpoint and usage
events, transcript compaction and cache sealing, tool result appending) in the same order; and the
same empty-result fallback and failed-result write on error. The public return contract of
`handle_task` (the list of pending events) SHALL be unchanged.

#### Scenario: A representative task run yields equivalent output

- **WHEN** a representative task with a scripted sequence of model responses and tool results is
  driven through the migrated `handle_task` and through the pre-migration `handle_task` with
  identical inputs
- **THEN** the returned pending events, the published result, accumulated usage, and trace are
  equivalent

#### Scenario: Cleanup runs on every exit path

- **WHEN** the task ends — normally, via the round limit, via exhausted recovery, or via an
  exception propagating out of a node
- **THEN** the trace span closes, the heartbeat stops, the temporary workspace is removed, the
  incoming-message queue is drained, and per-task fields are reset, exactly as before

### Requirement: The task's scope is parsed once at the run boundary

`handle_task` SHALL parse `task["scope"]` into an `OwnerScope` exactly once — via `from_dict`,
with defaults applied only at the transport mint point — and SHALL hand it to `build_task_run`,
which places it on the run configuration and thereby on `RunState.scope`. The agent SHALL read
identity strictly from the run state, and heartbeat, progress, typing-stop, and error paths
SHALL take chat and surface from `state.scope`.

The agent holds no module-level mutable references to the current chat, task source, or transport.

#### Scenario: Scope is parsed once and placed on the run state

- **WHEN** `handle_task` runs a task whose dict carries a serialized scope
- **THEN** `RunState.scope` holds the deserialized `OwnerScope`
- **AND** the parse happens at a single code path

#### Scenario: Agent lifecycle paths read identity from the run state

- **WHEN** the heartbeat, progress, typing-stop, and error paths run during a task
- **THEN** each reads chat and surface from `RunState.scope`, and no agent attribute such as
  `_current_chat_id` exists to read

#### Scenario: A task without a serialized scope is handled deterministically

- **WHEN** a malformed task dict reaches the agent without a `scope` key
- **THEN** the run fails with a descriptive error rather than guessing an identity
- **AND** every production task-producing path (orchestrator and supervisor-built tasks) mints a
  scope before the queue, so this is a programming-error path, not a user path

### The supervisor addresses runs through stored records

The agent SHALL NOT expose any accessor that reveals the active run's scope to the
supervisor or any other process-local consumer. The supervisor addresses runs through
stored records (`TaskRecord`), not process memory. See `run-addressing` spec.

