# DEVELOPMENT.md — Engineering handbook (headless)

## What this file is

Engineering procedure for changing the current system map ([ARCHITECTURE.md](ARCHITECTURE.md)).

| Document | Role |
|----------|------|
| AGENTS.md | Short routing guide: what to read, test-first workflow, boundaries, cleanup budget |
| DEVELOPMENT.md (this file) | How — patterns for safe changes |
| ARCHITECTURE.md | What exists in the headless runtime today |
| Wiki [Solution Architecture](https://gitlab.k8s.genai.int/gen-ai/fusion_team/backend/ouroboros/-/wikis/Solution-Архитектура-As-Is) | Production target — Secman, sandbox, audit, MVP0 |

This file is **not** a changelog.

`AGENTS.md` stays a short routing guide, this file owns engineering procedure, and `ARCHITECTURE.md` owns the operational map. Link instead of copying sections.

---

## Naming convention

- **Language:** English identifiers, comments, commit messages.
- **Style:** PEP 8 — `snake_case` modules/functions, `PascalCase` classes, `UPPER_SNAKE_CASE` constants.
- **Names** should state behavior, not abbreviations.

### Package and module navigation

- Organize by real subsystem, then cohesive capability; three or more related prefix siblings are a signal to consider a package.
- Package moves are explicit, behavior-preserving structural work with import tests. Do not add empty/one-file packages for symmetry.
- Avoid ambiguous `core.py`, `utils.py`, `manager.py`, or `state.py` without a clear package owner.
- Keep `__init__.py` exports narrow; import concrete owners. Keep nearest tests discoverable from production paths.

### Entity types

| Type | Pattern | Logic? | Example |
|------|---------|--------|---------|
| Gateway | `{Platform}Gateway` | Transport only | (reserved; add under `ouroboros/gateways/` when needed) |
| Service | `{Domain}Service` | Orchestration | — |
| Tool | `{verb}_{noun}` | Thin LLM surface | `data_read`, `skill_exec` |

```
LLM → Tool → (Gateway | filesystem | subprocess)
```

---

## Scoped Refactoring

Priority: requested behavior → correctness/security → tests → optional cleanup.

| Mode | Use when | Limit |
|---|---|---|
| Required | A test seam, owned policy, or safe lifecycle/error handling otherwise cannot be implemented | Smallest seam; no silent public-contract expansion |
| Adjacent cleanup | Requested behavior works and touched code contains one obvious debt item | One concern; owner + nearest tests + at most two adjacent modules; same narrow tests |
| Broad | Packages, persistence, dependencies, ABC/DI, ownership, or unrelated suites change | Explicit request and separate plan |

Adjacent means a direct caller or callee in the same runtime flow. Optional cleanup adds no dependency, migration, compatibility layer, public API, or cross-package move. If it needs another test matrix or design decision, defer it.

### Agent decision procedure

1. Read `AGENTS.md`, the owner, and nearest tests; add the narrowest failing test.
2. Implement requested behavior with the smallest safe change.
3. Check only the touched path against the debt ratchets below; do not start a cleanup search.
4. Apply at most one adjacent cleanup if the table permits it.
5. Run narrow tests and lint touched Python; expand only for a changed boundary.
6. Report requested work, cleanup, and deferred debt separately.

---

## Known Debt Ratchets

Debt is a non-growth baseline, not permission for a repository audit. Fix a touched violation only within the table above; otherwise add no new instance and report it.

| Concern | Ratchet |
|---|---|
| Redaction | Converge secret keys, token/URL patterns, and recursive redaction on `policy/redaction.py`; all sinks share tests and fail closed |
| Commands | Use one parser and compare the exact command token; `/restartXYZ` is not `/restart` |
| Dependencies | Flow `app/gateway/ingress → supervisor → agent → llm/tools/policy/storage`; replace legacy reverse imports with narrow ports/events |
| Deferred work | Require owner, consumer, durable state when needed, cancellation/error handling, and completion test; an audit marker is not a job |
| Settings | Known `AppSettings` fields use attributes; extras use an explicit API such as `extra_fields()` |
| Imports | No local imports inside loops; local imports require an optional dependency or documented cycle/startup reason |
| Errors | No silent broad catches, secret-bearing fallbacks, or fail-open sanitizers; preserve causes |
| Abstractions | No policy-free pass-through wrappers or speculative ABC/service/manager layers; require duplication removal, I/O isolation, or a test seam |
| Packages | Do not extend a cohesive flat prefix family indefinitely; propose its move as explicit structural work |

---

## Core Governance Artifacts

`AGENTS.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are core governance artifacts. Normal runtime task context MUST NOT inject them; production prompts contain task/runtime knowledge, not repo governance. Read them explicitly in coding/review workflows. Do not add them to `agent/context.py` prompt assembly; keep exclusion tests.

---

## Headless-specific development rules

### Configuration

- Production config is **environment-only**; do not reintroduce `/api/settings` or persistent `settings.json` without an explicit security design (wiki: instance config vs PSI hardcode).
- Use `AppSettings` in `storage/settings/schema.py` and `apply_settings_to_env()` for hot keys.
- `OUROBOROS_ENV=development` enables local-only knobs such as `MCP_ENABLED`; skill discovery still uses only `OUROBOROS_REPO_DIR/skills`.
- The MCP server and remote A2A agent **lists** are not settings: they come from `security_config/mcp.py` and `security_config/a2a.py`, imported once at startup and branching on `OUROBOROS_POLIGON`. The `DEV` branch delegates to the legacy `MCP_SERVERS` / `A2A_REMOTE_AGENTS` keys, which nothing outside `security_config/` and `schema.py` may read (`tests_headless/test_security_config_ssot.py`).

### HTTP / gateway

- New **browser** routes belong in `gateway/router.py` only with an explicit product decision; headless production keeps the minimal set documented in ARCHITECTURE.md.
- Update `gateway/contracts.py` when adding TypedDict fields used by `/api/health` or other gateway payloads.
- Run `tests/test_gateway_parity.py` and `tests_headless/test_headless_runtime.py` after route changes.

### Ingress

- Dialogs / SberChat paths: `supervisor/messaging/`, `supervisor/server_runtime.py`, future `ouroboros/ingress/sberchat/`.
- Inbound messages must go through `message_bus` / `LocalChatBridge`, not ad-hoc globals.

### Tools and policy

- Every new built-in tool: module under `ouroboros/tools/` with `get_tools()` and one `ToolSpec` entry in `ouroboros/tools/catalog.py`. `policy/safety.py::TOOL_POLICY`, core/discoverable/excluded surface, parallelism, and result caps are derived from that catalog.
- Agent service tools belong under `ouroboros/tools/service/`; keep task-facing tools and service/runtime helpers separate.
- Prefer the `@tool(...)` decorator from `ouroboros/tools/spec.py` for new simple tools; keep handlers thin and put shared guards outside individual tools.
- Do **not** add repo-mutation or arbitrary shell tools to the headless core set without wiki + security review.
- Skill subprocess execution stays in `tools/skill_exec.py`; extend sandbox there toward wiki `SandboxPolicy`.

### Skills

- Payloads live only under the trusted repository `skills/<name>/`; runtime code must not seed or scan `data/skills` buckets.
- Treat the production repository as read-only. Make payload changes through the release repository, not runtime repair tasks.
- Distribution manifests are the capability/settings allowlist. Keep extension load fail-closed and expose actual live readiness separately from discovery.
- Stage in-process extension imports under `/tmp`; durable `data/state/skills/<name>/` is for private extension state only.

### Review and commit protocol (this branch)

**Repo commit review, triad/scope review, and advisory pre-commit are not part of the headless distribution.** Do not document or reintroduce `repo_commit` / `advisory_pre_review` flows here without restoring the full review toolchain.

---

## Platform abstraction rule

All platform-specific syscalls go through `ouroboros/platform_layer.py`.

**JSON state:** prefer `utils.atomic_write_json` / `read_json_dict` with locks from `platform_layer`.

**Scanned by** `tests/test_platform_guard.py` for `ouroboros/`, `supervisor/`, `server.py`.

**Subprocess isolation:** use `platform_layer.subprocess_new_group_kwargs()` / `subprocess_hidden_kwargs()` — not raw `start_new_session` in feature code.

---

## Testing expectations

### Pre-merge quality gate (CI parity)

GitLab **check**, **test**, and **unit-regression** jobs run:

```bash
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy ouroboros supervisor
uv run pytest
uv run pytest tests/ -m "not integration and not browser"
```

- **Ruff / mypy:** configured in `pyproject.toml`. Ruff: E/W/F, isort, **UP** (PEP 585/604), bugbear, simplify, Ruff extras. Mypy: `ignore_missing_imports = false` globally; optional deps use `[[tool.mypy.overrides]]` (`a2a.*`, `dialog_bot_sdk.*`, `openai`, …). `disable_error_code` is empty — `attr-defined`, `union-attr`, `no-redef`, `misc`, and `import-untyped` are all enforced; `arg-type`, `assignment`, `return-value`, `index`, and `call-overload` are enforced. The config-driven local scope is `server.py`, `ouroboros`, `supervisor`, and `new_core`, including bodies of untyped functions; the current CI job still passes its explicit package arguments.
- **Pytest:** `testpaths = ["tests_headless", "tests", "skills"]`; the aggregate CI job covers all three paths and `unit-regression` repeats `tests/`. `integration` and `browser` markers are excluded unless explicitly selected.
- **Coverage:** no enforced `pytest-cov` threshold — add focused tests for new behavior and failure modes.
- **Docs:** update `docs/ARCHITECTURE.md` when current structure or operations change, and `docs/DEVELOPMENT.md` when engineering policy changes; run `tests_headless/test_governance_docs.py`.

### Suites

- Default pytest excludes `integration` and `browser` (see `pyproject.toml` markers).
- **Production profile:** `tests_headless/` — deploy env, router surface, forbidden tools.
- **Unit/regression:** `tests/ -m "not integration and not browser"` — required in CI; run targeted files first when editing an area.
- **Skills:** `skills -m "not integration and not browser"` — distribution-owned skill and extension behavior.
- **Browser (manual):** `uv run pytest tests/ -m browser` on a host with Playwright and Chromium.
- **Integration (manual):** `uv run pytest tests/ -m integration` with the required services and credentials.
- Contract changes: `tests/test_contracts.py`.
- Platform: `tests/test_platform_guard.py` for cross-platform syscall rules.

---

## Pre-merge checklist

- [ ] Narrow behavior/regression test exists; relevant tests, Ruff, and mypy pass.
- [ ] Optional cleanup obeys adjacency/one-concern rules and adds no ratchet violation.
- [ ] New modules have one owner and no speculative layer/package.
- [ ] Tools are catalogued/policy-covered and allowed in the headless surface.
- [ ] Platform and skill paths use `platform_layer` and `skill_payload_policy`.
- [ ] Runtime LLM calls use `ouroboros/llm/LLMClient`, not ad-hoc provider SDKs.
- [ ] Architecture, engineering policy, and target-security changes update their owning docs/wiki.
- [ ] Requested work, cleanup, checks, and deferred debt are reported separately.
