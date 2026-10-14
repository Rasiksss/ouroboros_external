---
paths:
  - "ouroboros/tools/**"
  - "ouroboros/policy/**"
---

# Tools and policy — read before editing

| Concern | Owner |
|---|---|
| Tool registration — one `ToolSpec` entry per tool | `ouroboros/tools/catalog.py` |
| Runtime lookup | `ouroboros/tools/registry.py` |
| `@tool(...)` decorator for new simple tools | `ouroboros/tools/spec.py` |
| `TOOL_POLICY`, core/discoverable/excluded surface, parallelism, result caps | `ouroboros/policy/safety.py` |
| Cross-cutting dispatch gates (excluded surface, safety supervisor) | `ouroboros/tools/dispatch.py` |
| LLM middleware factories (GuardrailsMiddleware etc.) | `ouroboros/policy/llm_middlewares.py` |
| Agent service/runtime helpers (kept separate from task-facing tools) | `ouroboros/tools/service/` |

**Rules:** every new built-in tool is a module under `ouroboros/tools/` with `get_tools()` plus one
catalog entry — policy and surface are *derived* from the catalog, never declared twice. Handlers
stay thin; shared guards live outside individual tools. Update the prompt and tests with the schema.
Do not add repo-mutation or arbitrary-shell tools to the headless core set without security review.

Skill-aware providers receive the required immutable `SkillSurface` from the composition root and do
not walk the filesystem or refresh `SkillCatalog`; `build_task_run` uses the startup-composed catalogs.
Delete this rule only if skill-backed tools stop deriving from skill packages.

**Redaction** is a debt ratchet owned by `docs/DEVELOPMENT.md` §Known Debt Ratchets. Short form:
add no new redaction site.
