---
paths:
  - "new_core/**"
  - "ouroboros/agent/task_graph.py"
  - "ouroboros/agent/loop_adapters.py"
---

# new_core: graph, loop, skills, tools — read before editing

An active migration boundary, not its final home (`docs/ARCHITECTURE.md` §Active new_core
migration). Invariant 4 in `docs/INVARIANTS.md` governs this package — read it
before changing loop or graph behavior.

| Concern | Owner | Import rule |
|---|---|---|
| Typed graph engine: topology is data, `build()` validates, `invoke` runs to `END` | `new_core/graph/` | stdlib only (`tests/new_core/graph/test_import_boundary.py`) |
| The LLM-with-tools round: nodes, routers, `GraphContext`, `RunState`, the ABC interfaces | `new_core/loop/` | stdlib + `new_core` only (`tests/new_core/loop/test_import_boundary.py`) |
| `LLMMessage` / `ToolCall` wire types, `add_usage` | `new_core/llm/` | stdlib only |
| Prompt-section declarations, the assemble/shrink/flatten walk | `new_core/prompt/` | stdlib only (`tests/new_core/prompt/test_import_boundary.py`) |
| `ToolCatalog`, `IToolProvider` / `ToolDescriptor`, `IToolRuntime`, `ToolContext`, `ToolSurface` | `new_core/tools/` | **exempt** — may import `ouroboros` (`tests/new_core/test_import_boundary.py` pins the exemption) |
| Skill models, interfaces, manifest, catalog, surface and DI providers | `new_core/skills/` | no application imports (`ouroboros`, `transport`); the manifest parser uses PyYAML |
| Identity: `OwnerScope`, `ServiceScope`, `service_identity` | `new_core/identity/` | no application imports |
| Per-task context and state, and the adapters that implement the loop ABCs | `ouroboros/agent/task_graph.py`, `ouroboros/agent/loop_adapters.py` | the bridge from ouroboros |

**Rules:**

- **No glue node.** Both graphs are authored in `new_core/loop/build.py` and every node lives in
  `new_core/loop/nodes.py`; nothing in `ouroboros/agent/` registers a node. A node that needs a
  new effect gets a loop interface first.
- **One task path.** Production builds the graph once through `build_task_graph` and calls `invoke`
  once. No second loop entry point, no legacy agent-loop module, one `ToolCatalog` per process with
  `ToolContext` passed as an argument rather than held.
- Nodes reach the outside world only through the ABCs on `GraphContext`. Tests fake loop effects by
  patching adapter methods — the same boundary nodes call — never a loop-module attribute.
- When the same logic appears on both sides of the seam, the canonical body **moves down** into
  `new_core/` and `ouroboros` imports it. No compatibility shim, no alias, no "keep the two in
  step" comment; the reverse import is already allowed and the ban is one-directional.
- Migration work stays inside these owners plus `tests/new_core/`,
  `tests/ouroboros/agent/test_loop_graph_*`, and `tests/ouroboros/agent/test_task_graph.py`.
- `ToolContext.skill_surface` is fail-closed when absent; skill-backed providers receive the required
  deployment `SkillSurface` at composition and never discover packages themselves. Delete this
  rule if those consumers leave `new_core/tools`.
