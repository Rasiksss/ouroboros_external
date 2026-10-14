---
paths:
  - "multiuser_config/supervisor/**"
  - "multiuser_config/*.py"
---

# The supervisor tier as its own process — read before editing

The third composition root. Composes the supervisor tier and nothing that runs a task (`docs/ARCHITECTURE.md` §Process model). The loop itself is `supervisor/` — see `.rules/supervisor.md`.

| Concern | Owner |
|---|---|
| Composition: shared start-up, collaborators, routes, loop's thread | `multiuser_config/supervisor/app.py` |
| One substitution point: gateway, agent-event consumer, work signal | `multiuser_config/supervisor/dependencies.py` |
| Serving and exit code | `multiuser_config/supervisor/__main__.py` |
| Shared start-up, address, serving | `multiuser_config/bootstrap.py` |
| Refusal set every root runs | `ouroboros/app/multiuser_validation.py` |
| Broker backend selector | `ouroboros/app/broker_composition.py` |

**Rules:**

- Nothing here imports what runs a task (`ouroboros.agent_runtime`, `ouroboros.agent`, `new_core.tools`, `ouroboros.mcp`, `ouroboros.egress.a2a`) — test parses import graph. `supervisor` *is* imported by design.
- Every refusal is the shared one, made by `bootstrap_process` in its order. This root adds no check of its own.
- Publish on `app.state` only what a mounted route reads (`IHealthProbes` only). `tests/multiuser_config/test_supervisor_process.py` asserts the set.
- The probes report the loop this process hosts and nothing about another tier. The database belongs on readiness (a tick that cannot read the queue answers with backoff), off liveness (that loop resumes by itself).
- No owner-bearing figure on a probe — queue depth, running counts and spend belong to the shared runtime database.
- Serve the configured address or exit non-zero. No free-port scan, no port file, no killing whoever holds a port.
- Nothing here restarts this process — the restart hook refuses, naming the tier.
- Every wait is bounded and owned.
- One root per process.
