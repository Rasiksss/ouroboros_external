# AGENTS.md

Read this short routing guide first; it points to owners/tests without duplicating `docs/ARCHITECTURE.md` or `docs/DEVELOPMENT.md`.

## Start Here

- Default workflow: read this file, then read only the touched module and its nearest tests.
- Tests first: write or update the narrowest failing pytest that names the behavior you are changing.
- Open `docs/ARCHITECTURE.md` only when changing module layout, lifecycle, persistent data shape, HTTP/WS contracts, tool lifecycle, or cross-module flows.
- Open `docs/DEVELOPMENT.md` when adding a new component type, changing engineering rules, or when a boundary is unclear.

## Project Map

- `server.py`, `ouroboros/app/`: entrypoint, startup, lifespan, composition.
- `ouroboros/gateway/`, `ouroboros/ingress/`: HTTP/WS and external-message boundaries; contracts live in `gateway/contracts.py`.
- `ouroboros/agent/`: task execution, prompt context, memory, retrieval, reflection, and response guards.
- `ouroboros/tools/`, `ouroboros/skills/`: thin LLM tools and skill lifecycle.
- `ouroboros/policy/`, `ouroboros/llm/`: safety/authorization and provider/model routing.
- `ouroboros/storage/`, `ouroboros/contracts/`: persistence/settings and stable shared shapes.
- `ouroboros/observability/`: logging, events, health, pricing, traces.
- `supervisor/`: queue, workers, orchestration, runtime state, events, messaging.
- `tests_headless/`: default production suite; `tests/`: focused suites run explicitly.

## Edit Map

If you change one of these surfaces, read these owners first:

- HTTP/WS payload: `ouroboros/gateway/contracts.py`, `router.py`, relevant adapters, parity/browser tests.
- Tool schema/behavior: owner in `ouroboros/tools/`, `catalog.py`, `registry.py`, `ouroboros/policy/safety.py`, prompt, tests.
- LLM routing: `ouroboros/llm/client.py`, `models.py`, `registry.py`, `providers/`, `provider_models.py`, tests.
- Logs/events: owners in `ouroboros/observability/`, `supervisor/events.py`, matching tests.
- Settings/defaults: `ouroboros/storage/settings/`, `ouroboros/app/config.py`, startup wiring, tests. Per-owner overrides live in `storage/settings/user_settings.py`.
- Skills/extensions: `ouroboros/skills/`, `ouroboros/tools/skill_*.py`, matching tests.
- Supervisor/orchestration: relevant `supervisor/*.py`, `server.py` only for wiring, matching tests.
- Durable named memory (identity, facts, knowledge: one runtime-DB table keyed by `kind`; the index is a query, never a file): `ouroboros/storage/core_memory/memory/`, `migrations/versions/0006_core_memory.py`, `ouroboros/tools/memory_tools.py`, `ouroboros/agent/memory_recall.py`, `ouroboros/agent/context.py` section builders, the `_migrated_memory_owner` refusal in `ouroboros/tools/core.py`, `domain/models.py` (owns `identity_self_edit_enabled()`, the identity self-edit switch — hardcoded off, no setting behind it), matching tests.
- Scratchpad working memory (blocks in the runtime DB, not blob storage): `ouroboros/storage/core_memory/scratchpad/`, the shared migration, `ouroboros/tools/service/memory.py`, matching tests.
- Core-memory plumbing (SQL adapter base): `ouroboros/storage/core_memory/_shared/` — changing it touches every capability. The runtime DB engine behind it is `ouroboros/storage/runtime_engine.py`, shared with user settings.
- Persistent data: grep every reader/writer, then update all of them, tests, and architecture docs together.

## Architecture Boundaries

- `server.py` owns startup/lifecycle/wiring; gateway, ingress, and provider adapters own transport, not business rules.
- Tools validate, delegate, and format; shared config/path/env defaults stay in their existing SSOT.
- Dependencies point boundary → supervisor → agent → llm/tools/policy/storage; add no reverse import into `supervisor`.
- Extend an existing owner before adding a layer. New abstractions need current, measurable value.

## Coding Rules

- Write the minimum explicit code that makes the new test pass; prefer small functions to new classes/frameworks.
- Reuse, simplify, or delete before adding helpers. Abstract only to remove real duplication, isolate I/O, or create a required test seam.
- Type changed public functions; use named shapes instead of repeated `dict[str, Any]` payloads.
- Prefer `pathlib`, early returns, narrow exceptions, small pure helpers, and few mode flags.
- Comments explain why, not what.

## Scoped Refactoring

1. Priority: requested behavior → correctness/security → tests → optional cleanup.
2. Required refactoring is the smallest change needed for a test seam, policy reuse, or safe lifecycle/error handling.
3. Optional cleanup: at most one concern, the owner + nearest tests + at most two adjacent production modules, behavior-preserving, and proven by the same narrow tests.
4. Adjacent means a direct caller or callee in the same runtime flow. Do not search for cleanup beyond the touched path.
5. If it needs another package, dependency, migration, public contract, or test plan, defer and report it. Broad refactoring requires an explicit request.
6. Apply the debt ratchet in `docs/DEVELOPMENT.md`: do not add a known violation; report requested work, cleanup, and deferred debt separately.

## Reliability Rules

- Move heavy sync work off the event loop. Every async task needs an owner, cancellation, and error collection.
- External I/O needs a timeout and/or bounded retry at its owner.
- Never silently swallow errors; use narrow exceptions and preserve translated causes.
- Durable/log/schema changes update every reader and writer. Safety/auth/path changes inspect every enforcement point.
- Reuse atomic/path helpers; keep boundary outputs deterministic and stable across success/error paths.
- Use project logging, never secrets or raw auth payloads; mark unavoidable truncation explicitly.
- Add no security/command/settings SSOT, fail-open sanitizer, consumerless job, import inside a loop, or policy-free wrapper.

## Test, Document, Report

- Run the narrowest test first; final verification runs `uv run pytest tests_headless -q`, `uv run pytest tests -m "not integration and not browser" -q`, and `uv run pytest skills -m "not integration and not browser" -q` once each.
- Run `uv run ruff format --check .`, `uv run ruff check <touched paths>`, and config-driven `uv run mypy`.
- Isolate with `tmp_path`/`monkeypatch`, never real `~/Ouroboros/data`; reuse test helpers.
- Bug fixes need regression coverage; behavior changes cover the main and one failure/edge path.
- Update `docs/ARCHITECTURE.md` for structure/contracts/lifecycle/data flow; `docs/DEVELOPMENT.md` for engineering policy.
- Keep browser contracts in `ouroboros/gateway/contracts.py` plus parity tests; keep business logic out of boundary/registration code.
- Add no convenience dependency or parallel abstraction for an owned concern.
