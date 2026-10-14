# AGENTS.md

Routing guide: what to read, where to test, what not to touch. Does not duplicate
`docs/ARCHITECTURE.md` or `docs/DEVELOPMENT.md`.

## What this is

Ouroboros is an autonomous agent runtime: a supervisor queues tasks, an agent loop executes them
against a tool catalog, and memory, skills, and policy sit around that loop. Headless profile —
SberChat/Dialogs ingress, health-only HTTP surface, environment-only configuration.

Read `docs/INVARIANTS.md` before changing runtime behavior. **Violating one is a bug even if tests
pass.** Open `docs/ARCHITECTURE.md` for structure, `docs/DEVELOPMENT.md` for engineering policy.

`docs/ARCHITECTURE.md` is an operational map: package layout, import rules, data flow tables,
runtime profile. Not a place for behavioral specs (→ `openspec/`), editing constraints (→ `.rules/`),
engineering policy (→ `docs/DEVELOPMENT.md`), or invariants (→ `docs/INVARIANTS.md`).

## Task workflow

- Search `BACKLOG.md` by the touched path and terms; read only matching records. Deferred work
  stays out of scope until its revisit condition holds. Do not edit it unless explicitly asked.
- Before changing behavior covered by `openspec/specs/`, open an OpenSpec change before implementation
  (`/opsx:propose` where available; otherwise use the repository's OpenSpec workflow).
- When resolving a material uncertainty, append the decision to `HANDOFF.md` using
  `docs/DEVELOPMENT.md` §Decision handoff. Search only relevant entries; never load the whole log.
- For deployment and stand work, start at `docs/DEVELOPMENT.md` §Operational entrypoints.

## Stack

Python 3.12, asyncio. FastAPI + Starlette + uvicorn. SQLAlchemy 2.0 + Alembic + psycopg3. Pydantic 2.
openai>=1.0 SDK, mcp>2.0, a2a-sdk>1.0. Playwright + playwright-stealth, kafka-python, boto3, redis.
Package manager **uv**.

## Commands

```bash
uv sync --frozen                                          # install (CI parity)
uv run ouroboros-server                                   # run the server
uv run ouroboros                                          # CLI
uv run pytest tests_headless                              # production profile
uv run pytest tests -m "not integration and not browser"  # unit/regression
uv run pytest skills -m "not integration and not browser" # distribution skills
uv run ruff check <touched paths>                         # inner loop, while editing
./scripts/check.sh                                        # THE gate: format, lint, types, all suites
./scripts/check.sh --fast                                 # same without the suites
```

Browser and integration suites are manual: `-m browser` / `-m integration`.

## Commit convention

On `feature/*` branches, commit subject must name both tickets — Jira first, GitLab issue in parentheses:

```
GGGNT-123 (#456) Workstation tracing added
```

The `#` is required: GitLab turns `#456` into a link. Only enforced on `feature/*` branches via `commit-msg` hook; `main`, `devfix/`, merges, and rebases are exempt. Ticket numbers are not in this environment — ask the user before committing on `feature/*` branches.

## Project Map

- `server.py`, `ouroboros/app/`: entrypoint, startup, lifespan, composition.
- `ouroboros/gateway/`: mounted HTTP router and default health surface.
- `transport/`: user-facing transports (dialogs, browser, a2a, skill bridge) behind the seam in `transport/interfaces.py`. Imports the seam, never `supervisor`.
- `ouroboros/agent/`: task execution, prompt context, memory, retrieval, reflection, response guards.
- `new_core/`: graph, loop, skill, and tool substrate — active migration target (`ouroboros/agent/loop_adapters.py` is the bridge).
- `ouroboros/scheduling/`: owner-scoped schedules in the runtime DB.
- `ouroboros/policy/`, `ouroboros/llm/`: safety/authorization and provider/model routing.
- `ouroboros/mcp/`, `ouroboros/sandbox/`: MCP client surface and skill subprocess isolation.
- `security_config/`: MCP/A2A allowlists, header collector declaration, deployment prompt. SSOT, not settings.
- `ouroboros/storage/`, `ouroboros/contracts/`: persistence/settings and stable shared shapes.
- `ouroboros/observability/`: logging, events, health, pricing, traces.
- `supervisor/`: queue, workers, orchestration, runtime state, events.
- `background_memory/`: memory work off the task queue. Imports neither `supervisor` nor `ouroboros.agent`.
- `multiuser_config/`: multi-user tiers, one composition root each (enumerated in `multiuser_config/tiers.py`).
- `config_registry/`: agent registry, chat registry, owner settings — routers in one package, no transport imports.
- `infra/`: shared infrastructure — async engine, Redis client, Kafka client, cache boundaries, security context, personal MCP tokens. Alembic lives here too (`infra/migrations/`, `alembic.ini` sets `script_location`); every tier's start-up reads its head from the same place.
- `ouroboros/ingress/`, `ouroboros/egress/`: inbound and outbound message paths.
- `ouroboros/agent_events/`: seam between a run and whoever consumes what it reports.
- `ouroboros/agent_runtime/`: claims tasks and runs them; the supervisor imports none of it.
- `prompts/`: prompt payloads. `openspec/`: capability specs. `skills/`: trusted read-only distribution skill payloads.
- `tests_headless/`: default production suite; `tests/`: focused suites run explicitly.

## Edit Map

Owner lists live in `.rules/*.md`, one per subsystem, keyed by path in their `paths:` frontmatter.
The table is a starting map; `paths:` is the complete routing source.

| You are changing | Read first |
|---|---|
| `supervisor/**` | `.rules/supervisor.md` |
| `new_core/**` | `.rules/new-core.md` |
| `ouroboros/agent/**` | `.rules/agent-loop.md` |
| `transport/**` | `.rules/transport-seam.md` |
| `multiuser_config/transport/**` | `.rules/transport-process.md` |
| `config_registry/agent_registry/**` | `.rules/agent-registry.md` |
| `config_registry/chat_registry/**` | `.rules/chat-registry.md` |
| `infra/**` | `.rules/infra.md` |
| `infra/migrations/**` | `.rules/persistent-data.md` |
| `config_registry/user_settings/**` | `.rules/user-settings.md` |
| `multiuser_config/config_registry/**` | `.rules/config-registry-process.md` |
| `multiuser_config/supervisor/**` | `.rules/supervisor-process.md` |
| `multiuser_config/agent_runtime/**` | `.rules/agent-runtime-process.md` |
| `multiuser_config/bootstrap.py` | `.rules/supervisor-process.md`, `.rules/agent-runtime-process.md`, `.rules/background-memory.md` — it is the start-up every tier shares |
| `transport/browser/**` | `.rules/http-contracts.md` |
| `ouroboros/gateway/**` | `.rules/http-contracts.md` |
| `ouroboros/ingress/**` | `.rules/ingress-egress.md` |
| `ouroboros/egress/**` | `.rules/ingress-egress.md` |
| `ouroboros/scheduling/**` | `.rules/scheduling.md` |
| `ouroboros/tools/**` | `.rules/tools-and-policy.md` |
| `ouroboros/policy/**` | `.rules/tools-and-policy.md` |
| `new_core/skills/**` | `.rules/skills.md` |
| `ouroboros/skills/**` | `.rules/skills.md` |
| `skills/**` | `.rules/skills.md` |
| `ouroboros/mcp/**` | `.rules/mcp-sandbox.md` |
| `ouroboros/sandbox/**` | `.rules/mcp-sandbox.md` |
| `ouroboros/llm/**` | `.rules/llm-routing.md` |
| `ouroboros/contracts/**` | `.rules/contracts.md` |
| `ouroboros/storage/core_memory/**` | `.rules/core-memory.md` |
| `ouroboros/storage/settings/**` | `.rules/settings.md` |
| `ouroboros/storage/conversations/**` | `.rules/conversations.md` |
| `ouroboros/storage/execution_memory/**` | `.rules/execution-memory.md` |
| `ouroboros/storage/tasks/**` | `.rules/tasks.md` |
| `ouroboros/storage/tool_confirmation/**` | `.rules/tool-confirmation.md` |
| `ouroboros/app/config.py` | `.rules/settings.md` |
| `security_config/**` | `.rules/settings.md` |
| `ouroboros/observability/**` | `.rules/observability.md` |
| `background_memory/**` | `.rules/background-memory.md` |
| `multiuser_config/background_memory/**` | `.rules/background-memory.md` |
| `tests/**` | `.rules/testing.md` |
| `tests_headless/**` | `.rules/testing.md` |

Each rule declares more paths than the one shown; the table is routing, not the full key.

**Codex and other agents:** before editing, run `uv run python scripts/resolve_rules.py <file>...`
with the planned repository-relative file paths, including new files. Then explicitly read every
matching rule it prints, using an available file reader or the shell (`sed -n`, `cat`, or Python).
The resolver reads all `paths:` declarations, including overlaps absent from this table; rerun it
when scope expands. Reading a source file does not load its rules. If the resolver cannot run,
inspect every rule's frontmatter and read all matches. Claude-specific loading is in `CLAUDE.md`.

**Rules are ownership maps and editing constraints, not behavioral docs.** A rule is updated in a
PR only when a file path moved, a new ownership boundary was introduced, or the rule itself was
wrong. Any other change is out of scope for a feature/fix PR. Rules are reviewed on a
**monthly cadence** by the subsystem owners. Rule form: `docs/DEVELOPMENT.md` §Core Governance Artifacts.

**Ask before editing governance artifacts.** `.rules/`, `AGENTS.md`, `docs/ARCHITECTURE.md`,
`docs/DEVELOPMENT.md`, and `docs/INVARIANTS.md` change only when a module is added, conceptually
restructured, or removed — not as a side effect of a feature or fix. Before editing any of them,
state which of these three reasons applies and wait for explicit confirmation. `CODEOWNERS` enforces
review; the instruction here enforces intent.

## Scoped Refactoring

Priority: requested behavior → correctness/security → tests → optional cleanup.
Adjacent means a direct caller or callee in the same runtime flow.
Do not search for cleanup beyond the touched path. The same narrow tests must pass; apply the
debt ratchet in `docs/DEVELOPMENT.md`: do not add a known violation; report requested work,
cleanup, and deferred debt separately. Full procedure: `docs/DEVELOPMENT.md` §Scoped Refactoring.

## Definition of Done

- [ ] A narrow test names the changed behavior and failed before the change.
- [ ] Bug fixes have regression coverage; behavior changes cover the main path and one failure/edge path.
- [ ] `./scripts/check.sh` exits 0 — format, lint, types, and all three suites.
- [ ] An independent review ran against the diff with a clean context — a fresh session or review
      agent, citing file:line. Self-assessment by the session that wrote the code is not review.
      Review must verify `docs/INVARIANTS.md` and `docs/DEVELOPMENT.md` §Coding Rules against the diff.
- [ ] Owning doc updated: `docs/ARCHITECTURE.md` for structure/contracts/lifecycle/data flow, `docs/DEVELOPMENT.md` for engineering policy.
- [ ] No comment or docstring restates a doc; whatever ran over the budget moved to its owning doc, and the code points at it.
- [ ] Requested work, optional cleanup, and deferred debt reported separately.

Not done: "code written and it looks right", "unit tests pass", "works on my machine".
Full gate: `docs/DEVELOPMENT.md` §Coding Rules, §Scoped Refactoring.
