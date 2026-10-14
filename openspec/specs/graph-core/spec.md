# graph-core Specification

## Purpose

A generic, dependency-free graph-execution core. Control flow is modeled as nodes that thread a
single mutable state object between them: a node is `fn(state, ctx)` that mutates `state` in place,
context is an immutable opaque per-run object, and topology is data (static edges and conditional
router-based edges). `build()` validates the topology; `invoke` then runs from the entry node until
`END`, returning the same state object mutated in place. The engine never inspects or copies state,
and depends only on the Python standard library — it imports nothing from `ouroboros`.

## Requirements

### Requirement: The engine is generic over Context and State

The graph-execution core SHALL be parameterized by a caller-defined immutable
`Context` type and a caller-defined mutable `State` type, and SHALL NOT depend on the
shape of either. The core SHALL NOT import from `ouroboros` or from any package outside
the Python standard library.

#### Scenario: State and Context shapes are opaque to the engine

- **WHEN** a graph is built and run with arbitrary caller-defined `Context` and `State`
  types
- **THEN** the engine passes `state` and `ctx` to nodes and routers without inspecting
  or copying them
- **AND** running the engine requires no import from `ouroboros`

### Requirement: Nodes mutate the run state in place

A node SHALL be a callable `node(state, ctx)` that mutates the shared state object in
place; its return value SHALL be ignored. The engine SHALL NOT merge, copy, or otherwise
transform state between nodes — it threads the same state object through the graph.

#### Scenario: A node's mutation is visible to the next node

- **WHEN** one node mutates a field of state and a later node reads that field
- **THEN** the later node observes the mutated value

#### Scenario: A node that mutates nothing leaves state unchanged

- **WHEN** a node runs without mutating state
- **THEN** the state observed by the next node equals the state before that node ran

### Requirement: Topology is defined by static and conditional edges

The graph SHALL support static edges via `add_edge(src, dst)` and conditional edges via
`add_conditional_edge(src, router)`, where `router(state, ctx)` returns the name of the
next node. Nodes SHALL NOT name their own successors; routing decisions live in routers
and static edges only.

#### Scenario: A static edge advances to a fixed successor

- **WHEN** a node with a static edge to `B` finishes
- **THEN** the engine runs node `B` next

#### Scenario: A conditional edge advances to the router's choice

- **WHEN** a node has a conditional edge whose router returns `"exec_tools"`
- **THEN** the engine runs the node named `exec_tools` next

#### Scenario: A router may direct execution to END

- **WHEN** a router returns the `END` sentinel
- **THEN** the engine stops and returns the current state

### Requirement: START and END are reserved sentinels

The core SHALL expose reserved `START` and `END` sentinels. Execution SHALL begin at the
node designated as the entry point, and SHALL terminate when an edge or router leads to
`END`. `START` and `END` SHALL NOT be usable as ordinary node names.

#### Scenario: Execution begins at the entry node

- **WHEN** a built graph is invoked
- **THEN** the first node run is the graph's declared entry node

#### Scenario: Reaching END terminates the run

- **WHEN** execution follows an edge to `END`
- **THEN** the engine returns the accumulated state without running further nodes

### Requirement: Building validates the topology

`build()` SHALL validate the graph before it can be invoked and SHALL reject an invalid
topology with a descriptive error. Validation SHALL reject: edges referencing an unknown
node, a missing or unknown entry node, and a node with no outgoing edge that is not
terminal. An invalid graph SHALL NOT be invokable.

#### Scenario: An edge to an unknown node is rejected at build time

- **WHEN** a graph declares an edge whose destination is not a registered node or `END`
- **THEN** `build()` raises a descriptive error
- **AND** no node is executed

#### Scenario: A missing entry point is rejected at build time

- **WHEN** a graph is built without a valid entry node
- **THEN** `build()` raises a descriptive error

#### Scenario: A valid graph builds to an invokable form

- **WHEN** a graph with a valid entry, reachable nodes, and edges terminating at `END`
  is built
- **THEN** `build()` returns an object that can be invoked

### Requirement: Execution threads state through nodes until END

Invoking a built graph SHALL run the entry node, let it mutate state, follow the
applicable edge, and repeat until reaching `END`, then return the state object it was
given. The state object handed to a node SHALL reflect every mutation made before that
node runs. The returned object SHALL be the same object passed to `invoke`.

#### Scenario: State accumulates across a multi-node path

- **WHEN** a graph runs entry → `B` → `END` where each node appends to a list field of
  state
- **THEN** the returned state's list contains both nodes' contributions in order

#### Scenario: invoke returns the same object it was given

- **WHEN** a state object is passed to `invoke`
- **THEN** the returned value is that same object, mutated in place

#### Scenario: A self-loop repeats until a router exits to END

- **WHEN** a node routes back to itself while a condition holds and to `END` once it does
  not
- **THEN** the engine runs that node repeatedly and returns once the router chooses `END`

### Requirement: A step budget guards against non-termination

Invocation SHALL accept a `max_steps` budget that caps the number of node executions
independently of any domain logic. Exceeding the budget SHALL raise a descriptive error
rather than running unbounded. This cap SHALL be distinct from `END`-based termination.

#### Scenario: Exceeding the step budget raises

- **WHEN** a graph that never reaches `END` is invoked with `max_steps` set to a finite
  value
- **THEN** the engine raises a descriptive error after that many node executions
- **AND** the error identifies the run as exceeding the step budget

#### Scenario: A run within budget completes normally

- **WHEN** a graph reaches `END` in fewer steps than `max_steps`
- **THEN** the engine returns the final state and does not raise
