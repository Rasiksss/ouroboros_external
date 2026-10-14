---
paths:
  - "background_memory/**"
  - "multiuser_config/background_memory/**"
  - "multiuser_config/bootstrap.py"
  - "tests/background_memory/**"
---

# Background memory — read before editing

Memory work off the task queue: the publish/consume seam, eligibility and thresholds, the distillation, chat-consolidation and titling handlers, and the loop they are delivered on. Asynchronous end to end; the package owns no thread.

| Concern | Owner |
|---|---|
| Seam, plus the `FollowUp` handler↔loop contract | `background_memory/interfaces.py` |
| Distillation eligibility and execution; the one sanctioned executor (a memory tool body) | `background_memory/handlers/distillation.py` |
| Consolidation: thresholds, batching, deferral, summary commit; the run in flight | `background_memory/handlers/consolidation.py` |
| Titling step and composite dispatch value | `background_memory/handlers/titles.py` |
| Every database read and write this package makes: lease, summary, chat backlog, memory headers, audit claim and outcome row, task reads | `background_memory/infra/repositories/` |
| Single-flight and failure cooldown (`consolidation_leases` row) | `background_memory/infra/repositories/lease.py` |
| Committed summary — one axis narrower than the lease: `(owner_id, agent_id, chat_id)` | `background_memory/infra/repositories/summary.py` |
| The owner's model keys a handler reads, through the reader's `async_` accessors | `config_registry/user_settings/reader.py` — see `.rules/user-settings.md` |
| Holder id an instance presents on leases | `background_memory/identity.py` |
| Dispatching loop: the task, per-pair serialization, the drain, the heartbeat | `background_memory/runtime.py` |
| Idle policy while the broker does not answer | `background_memory/infra/events/outage.py` |
| Event transport and publisher factory: in-process queue, Redis stream, `aiokafka` group member | `background_memory/infra/events/` |
| Wire form of an event, both directions | `background_memory/infra/events/codec.py` |
| `IForegroundPressure` over the supervisor's task table | `background_memory/infra/foreground.py` |
| Publishers | `ouroboros/agent/loop_adapters.py`, `supervisor/message_bus.py` |
| Dispatch table wiring | `server.py` |
| Tier as its own process | `multiuser_config/background_memory/run.py` |
| Shared start-up, address, probe server | `multiuser_config/bootstrap.py` |
| Tests | `tests/background_memory/`, the boundary in `test_import_boundary.py`, the loop guard in `test_nothing_blocks_the_loop.py` |

**Boundary:** imports neither `supervisor` nor `ouroboros.agent` — the dependency runs the other way — and no `ouroboros.storage` beyond seven modules named one by one (a prefix would let the runtime store, the engine and the blob provider in). It does import `config_registry.chat_registry`, which imports no transport either. Grep before adding any import, and read the allowlist's comments before adding to it.

**Rules:**

- The package owns no thread — not the loop, not a delivery, not a transport. The one sanctioned placement is the per-call executor a memory tool body runs on, named in `background_memory/handlers/distillation.py`; the body is abandoned past what is left of the run's deadline, so one hung body costs one thread of its own dead pool.
- Nothing blocks the loop: every handler is a coroutine, every model call goes through `async_chat_with_fallback`, everything from outside arrives through a seam this package owns. `test_nothing_blocks_the_loop.py` reads the call site and does not follow calls — a helper that blocks three frames down passes it.
- One async engine, one event loop. A pooled psycopg connection binds an `asyncio.Lock` to the first loop that used it, so both roots run the loop as a task on the loop that owns the engine (`multiuser_config/background_memory/run.py` on `asyncio.run`, `server.py` on the lifespan's).
- Data access is the repositories', each handed an `AsyncEngine` with no default and reading no setting to decide how to connect. Duplicating a statement `ouroboros/storage` also issues is deliberate; a statement only this package calls lives only here.
- Repository shape: `Sql<Thing>Repo`, `engine.connect()` to read and `engine.begin()` to write, every statement written out inside the `execute` that issues it, a module-level `_entity(row)`, the dialect through `engine.dialect.name`. Two methods needing one statement write it twice; a predicate travels with the statement it belongs to.
- Every read returning message text calls `decrypt_field` on every dialect and **propagates** `FieldKeyUnavailable`: it is the process's failure, not the row's, so a reader that skipped it would summarize a backlog with holes and commit a cursor past them. The run ends `failed` on its unmoved cursor and the pair's cooldown grows.
- Each delivery is a task of its own, and at most one per `(owner_id, agent_id)` runs at a time — correctness, not throttling: two deliveries for one pair share the run object and commit under one cursor the compare-and-swap cannot tell apart. Waiting polled deliveries are bounded (`MAX_WAITING_PER_PAIR`, newest dropped); a follow-up is exempt and goes ahead of them, because dropping one strands its run behind the single-flight guard until the lease expires.
- Stopping drains within one bound (`memory_drain_timeout_sec`, derived from the same read timeout the lease expiry is), cancels what outlasts it and gives the cancellations what it kept back (`CANCELLED_RELEASE_GRACE_SEC`). A stopping lane still serves its follow-ups — the only way a run releases its pair — and drops what merely waits; the release runs under `asyncio.shield` and a root awaits the loop's task before reporting the process stopped.
- What a consumer holds on the broker it releases itself: `IMemoryEventConsumer.close` is written by all three backends, so no root dispatches on a concrete transport. A root awaits it after the loop has drained.
- The consuming half is awaitable and the publishing half is not, on all three backends: five producing call sites outside this package hand events over from threads. The package names `kafka-python` nowhere — `infra/kafka_client.py` is the one description, and it builds clients of both libraries.
- The owner's settings are the settings reader's, through its `async_` accessors; this package owns no seam and no repository for them. A handler awaits the twin, never the synchronous accessor, which would read the database on the loop — the guard fails a call to anything named `OUROBOROS_*` here.
- A run's outcome row names the run's owner: the scope arrived on the event, and minting one is forbidden here. Both handlers build the `AuditEvent` and write it through `SqlAuditRepo.record`.
- The titling step reaches the chat registry's asynchronous face (`IChatRegistryRepo`); the synchronous `ChatRegistryStore` stays for the transport and schedule dispatch, which call from threads. The claim is still the statement's row count, taken before the model call.
- The persisted-message kind carries multiple steps (titling then consolidation). Do not route titling through the consolidation handler — its lease and thresholds are fatal for naming.
- The lease is wider than the row it guards: single-flight is per `(owner, agent)` while the summary is per `(owner, agent, chat)`. Do not narrow the lease — it holds an owner with many conversations to one background model call.
- A trigger for a second conversation arriving while the pair is leased is delayed, not lost.
- `pytest-asyncio` is not added here. A repository test seeds and drives through `tests/helpers/background_memory_db.py` (`db.run`), so its calls land on the loop its engine belongs to; a test with no repository drives a coroutine through a module-level `_run(coro)`. A third style is the defect.
- No probes, no loop; stop them after it has drained. Liveness is the oldest delivery's age against `lease_expiry_sec` — never the dispatcher's own pass, which would beat briskly while every delivery it started was hung, and never a bound stated for the probe alone (`tests/multiuser_config/test_worker_tier_probes.py`).
- Every refusal is the shared one, made by `bootstrap_process`. This root adds no check of its own.
