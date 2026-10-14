---
paths:
  - "supervisor/**"
---

# Supervisor and orchestration — read before editing

Queue, workers, orchestration, runtime state, and events live in `supervisor/`. Touch `server.py`
only for wiring.

| Concern | Owner |
|---|---|
| SSOT wiring | `supervisor/bootstrap.py` (`build_supervisor_runtime`) |
| Process-local runtime registry | `supervisor/runtime_context.py` (`SupervisorRuntime`) |
| Supervisor loop + context | `supervisor/runtime.py` (`SupervisorLoop`, `SupervisorContext`) |
| Task queue: enqueue, claim, timeout/lease sweeps | `supervisor/queue.py` (`TaskQueue`) |
| Publishing boundary, owner history | `supervisor/message_bus.py` (`LocalChatBridge`) |
| Event handlers (one per report kind) | `supervisor/events.py` |
| Orchestrator (ingress SSOT) | `supervisor/orchestrator/` (`TaskOrchestrator.submit`) |
| Slash commands | `supervisor/commands.py` |
| Tests | `tests/supervisor/` |

**Rules:**

- The terminal publication tick is bounded by wall clock (`_TERMINAL_TICK_BUDGET_SEC`). A test that shrinks it must leave the budget longer than the store round trips before the first publish. The fencing test in `tests/supervisor/test_supervisor_regressions.py` ties its elapsed bound to the budget it sets, not to a constant.
- A faked lease is that same bound, not a smaller one. `before_publish` re-reads `terminal_publication_remaining` and truncates the store deadline to it, so a stubbed lease shorter than the budget fences the publish out on a loaded runner. Stub the lease at a whole second like the budget.
- Boundary: dependencies point boundary → supervisor → agent → llm/tools/policy/storage. Add no reverse import *into* `supervisor` — `transport/` and `background_memory/` in particular must not import it. Deferred work needs an owner, a consumer, durable state where required, cancellation/error handling, and a completion test; an audit marker is not a job.
