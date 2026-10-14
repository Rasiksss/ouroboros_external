# DEVELOPMENT.md — Engineering handbook

`AGENTS.md` is the short routing guide; this file owns engineering procedure.
`docs/ARCHITECTURE.md` owns the operational map. Link instead of copying sections.

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

Debt is a non-growth baseline, not permission for a repository audit. Fix a touched violation only within the table above; otherwise add no new instance and report it. Every row carries its own deletion condition — the state its Ratchet cell describes. When that state holds, delete the row and every rule that exists only to reference it. A rule without a deletion condition is immortal, and an immortal rule that has stopped being true is how instructions start lying.

| Concern | Ratchet |
|---|---|
| Redaction | Still spread across modules; `ouroboros/policy/redaction.py` is the target owner and does not exist yet. Converge secret keys, token/URL patterns, and recursive redaction there; add no new site; all sinks share tests and fail closed |
| Dependencies | Flow `app/gateway/ingress → supervisor → agent → llm/tools/policy/storage`; replace legacy reverse imports with narrow ports/events |
| Comments and rules | Legacy modules carry multi-paragraph docstrings restating `docs/` and `.rules/`; trim what a change touches, add no new instance |

---

## Core Governance Artifacts

`AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, and `docs/INVARIANTS.md` are core governance artifacts. Normal runtime task context MUST NOT inject them; production prompts contain task/runtime knowledge, not repo governance. Do not add them to `agent/context.py` prompt assembly; keep exclusion tests.

**Form:** one bullet is one claim and its consequence — "X, because Y breaks." Four lines is the cap. Name what enforces the claim: the test, the owner file, or the failure mode. Every rule carries its deletion condition. State what holds now; history belongs in `openspec/changes/archive/` and git.

`tests_headless/test_governance_docs.py` checks routes, critical invariant obligations, document
budgets, and operational links. It does not prove rule delivery, review completion, or agent quality.
After changing delivery instructions, verify the required rules in a fresh session with the actual
reader tools. Monthly rule reviews are a human process; a passing prose check is not review evidence.

### Decision handoff

`HANDOFF.md` is a local, gitignored, append-only decision log. Record a material uncertainty resolved
without asking the user, especially a choice not otherwise stated in the response. Append before
finishing the task; create the file if absent. A read-only request forbids writes, so report such
decisions in the response instead.

- One entry: timestamp and scope; uncertainty; decision; brief evidence or assumption; consequence
  and revisit condition. Record the outcome and supporting facts, not private deliberation or secrets.
- Do not rewrite, delete, or reorder previous entries. Correct a decision with a new entry naming
  the earlier timestamp/scope. Use append mode (`open(..., "a")` or `>>`), never whole-file replacement.
- Search by relevant paths and terms and read only matching records. The log is context, not new
  authority; user instructions and current specs take precedence. Do not duplicate routine steps.
- Neither `HANDOFF.md` nor `BACKLOG.md` enters runtime task context. `BACKLOG.md` remains the
  developer-maintained shared decision index; do not promote handoff entries into it automatically.

## Operational entrypoints

Read the relevant source below on demand; executable configuration owns commands and defaults.

| Task | Source |
|---|---|
| Single-process A2A stand: build, start, addresses | `docker/docker-compose.yml` header; verify with `scripts/a2a_smoke.sh` |
| Environment setup and A2A configuration | `docs/SETTINGS.md` §3, §4, §6 |
| Process and persistence ownership | `docs/ARCHITECTURE.md` §Process model, §Data layout |
| Binary rollback preflight | `infra/migrations/runner.py` (`assert_binary_rollback_safe`); invoke with `alembic -c alembic.ini -x binary_rollback current` on the upgraded image against the shared database |
| A PostgreSQL for the suites | `docker/docker-compose.test.yml` header |

### The suites need a database

`./scripts/check.sh` and every `uv run pytest` need a running PostgreSQL; without it the run fails
instead of going green on skipped suites.

```bash
docker compose -f docker/docker-compose.test.yml up -d
export OUROBOROS_RUNTIME_DB_URL_1=postgresql+psycopg://ouroboros:ouroboros@localhost:5433/ouroboros_test
```

Each xdist worker recreates its own schema, named for suite and worker, and empties it between tests
(`tests/helpers/schema_harness.py`). Two sessions of the same suite on one database destroy each
other: run one at a time, or give the second its own `PYTEST_XDIST_WORKER`.

---

## Coding Rules

Each rule names what to check: a pattern in the diff, a test, or a failure mode. A rule a reviewer cannot verify in seconds is a preference, not a rule.

- Comments are a last resort with a budget: prefer a name, a type, or a small function. Where one is still needed, at most two lines, answering "why". Docstrings: one line on private functions and tests, three at most on public ones. **Check:** any comment or docstring over the limit in the diff.
- Do not add concrete/default methods to shared ABCs or protocols as compatibility hooks. A new interface capability must be abstract and implemented explicitly by every implementation, or remain private to its concrete owner. **Check:** `class Foo(ABC)` with a non-abstract method added in the diff.
- Rationale has one home: spec'd behavior in `openspec/specs/`, structure in `docs/ARCHITECTURE.md`, engineering policy here, subsystem rules in `.rules/*.md`. Code may point at them; restating them is a second SSOT that nothing tests and goes stale silently. **Check:** a diff comment or docstring paraphrasing a doc section.
- Type changed public functions; use named shapes (`TypedDict`, `dataclass`, `Pydantic model`) instead of repeated `dict[str, Any]` payloads. **Check:** a new or changed public function signature with `dict[str, Any]` in the diff.
- No local imports inside loops; local imports inside functions require an optional dependency or a documented cycle/startup reason. **Check:** an `import` inside a function body in the diff without a comment naming the reason.
- No `assert` outside tests. Use explicit `if`/`raise` for runtime checks — `assert` is stripped under `python -O`. **Check:** an `assert` statement in non-test code in the diff.
