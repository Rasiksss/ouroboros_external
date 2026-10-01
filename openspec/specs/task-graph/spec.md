# task-graph Specification

## Purpose

The agent's whole per-task lifecycle — context assembly, the LLM-with-tools loop, and result
emission — runs as a single built graph over the `new_core` engine, assembled once and invoked once
per task. Context assembly and tool binding are nodes at the head of the graph; result emission is
an effects-interface capability any node can invoke rather than a dedicated terminal stage; and
`Agent.handle_task` collapses to the lifecycle scaffolding that cannot be a node wrapped around one
`invoke`. The graph reuses the `loop-graph` nodes unchanged, and the task-phase nodes are ouroboros
glue so the `new_core/loop` layer stays free of `ouroboros` imports. The loop itself is specified in
`loop-graph`; the engine in `graph-core`.

## Requirements

### Requirement: The whole task lifecycle runs as one built graph

The agent's per-task work — context assembly, the LLM-with-tools loop, and result emission —
SHALL be expressed as a single graph over the `new_core` engine, built once and invoked once per
task. `Agent.handle_task` SHALL NOT contain the lifecycle *logic* itself; it SHALL reduce to the
scaffolding that cannot be a node (trace span, heartbeat, temporary workspace, and the `finally`
cleanup) wrapped around a single `invoke`. The graph SHALL be assembled once and reused across
tasks, not reassembled per task.

#### Scenario: A task runs by invoking the prebuilt task graph

- **WHEN** the agent handles a task
- **THEN** it invokes a prebuilt task graph rather than executing context-assembly, loop, and
  result-emission logic inline
- **AND** the graph is the same instance reused across tasks

#### Scenario: handle_task holds only lifecycle scaffolding around the invoke

- **WHEN** `handle_task` runs
- **THEN** the only work it performs outside `invoke` is the trace span, heartbeat, temporary
  workspace lifecycle, and the `finally` cleanup
- **AND** context assembly and result emission happen inside graph nodes

### Requirement: Context assembly is a graph node

Building the task's `ToolContext`, recalling relevant memories, and assembling the initial
transcript via `build_llm_messages` SHALL be performed by a `prepare_context` node at the head of
the task graph, which SHALL write the transcript and the tool context into `RunState`. Context
assembly SHALL NOT touch the tool registry.

#### Scenario: prepare_context populates run state before the loop

- **WHEN** the task graph runs from `START`
- **THEN** the `prepare_context` node builds the tool context, recalls memories, and builds the
  initial messages into `RunState`
- **AND** it neither installs that context on the registry nor derives tool schemas

### Requirement: Tool binding is a separate graph node

Binding tools to the prepared task — deriving the initial tool schemas from the tool catalog on
the graph context and this task's `ToolContext`, and attaching the tool surface to that context —
SHALL be performed by a `prepare_tools` node placed between `prepare_context` and the loop. The
node SHALL read the catalog from the graph context, not from a closure over the agent. The node SHALL
write the tool schemas, the active tool names, and the seeded pending overrides into `RunState`.
Loop nodes SHALL run only after `prepare_tools` has populated that state. The node SHALL NOT install
context on, or publish, a shared tool registry, and SHALL NOT replace tool handlers.

#### Scenario: prepare_tools builds the surface before the loop

- **WHEN** the task graph reaches `prepare_tools`
- **THEN** the initial tool schemas are derived from this task's `ToolContext`, not a previous task's
- **AND** the tool schemas and active tool names are available in `RunState` when the first loop
  node executes

#### Scenario: Initial surface selection lives in the node

- **WHEN** `prepare_tools` selects the starting set
- **THEN** the task constraint, attachments, configured tool names, and the extension schema budget
  are all applied by the node
- **AND** no provider decides which of its tools belong in the first request

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

### Requirement: handle_task threads the executor's stop signal into the run configuration

The stop signal is lifecycle scaffolding, not graph logic: `handle_task` SHALL accept an optional
stop callable from its caller and place it on the run configuration it builds, alongside the trace
span, heartbeat, and temporary-workspace handling it already owns. No graph node SHALL reach for the
signal directly.

The parameter SHALL be optional, so callers that do not manage a worker slot — the standalone loop,
the memory lanes, tests — are unaffected.

#### Scenario: The executor's per-task signal reaches the guard router

- **WHEN** the executor runs a task for a slot that holds a stop event
- **THEN** `handle_task` passes that event's predicate into the run configuration
- **AND** the guard router observes it between rounds

#### Scenario: A caller that supplies no signal is unaffected

- **WHEN** `handle_task` is called without a stop callable
- **THEN** the run configuration carries none and the task runs exactly as before

### Requirement: The loop layer stays pure; task-phase nodes are ouroboros glue

The `new_core/loop` layer — its loop nodes, routers, context, state, and interfaces — SHALL
continue to import nothing from `ouroboros`. The new task-phase nodes (`prepare_context`,
`prepare_tools`, finalize/emit) MAY import `ouroboros` directly and SHALL live under
`ouroboros/agent/`; the full
task graph SHALL be assembled on the ouroboros side from those glue nodes plus the reused, unchanged
`new_core/loop` nodes. New interfaces SHALL be introduced only where a node genuinely reaches
outside `new_core`.

#### Scenario: The loop layer has no ouroboros dependency after the migration

- **WHEN** `new_core.loop` and its submodules are imported
- **THEN** no import resolves to an `ouroboros` module
- **AND** the loop nodes reused by the task graph are unchanged

#### Scenario: Task-phase nodes are assembled on the ouroboros side

- **WHEN** the task graph is built
- **THEN** its `prepare_context`, `prepare_tools` and finalize/emit nodes come from `ouroboros/agent/`
- **AND** its loop nodes come from `new_core/loop`

### Requirement: Observable task behavior is preserved

Migrating the task lifecycle to the graph SHALL preserve `handle_task`'s observable behavior: for
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
