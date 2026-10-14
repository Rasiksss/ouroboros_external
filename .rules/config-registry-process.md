---
paths:
  - "multiuser_config/config_registry/**"
---

# The config registry as its own process — read before editing

A composition root. One FastAPI app with routers (`/api/agents`, `/api/owner-settings`, `/api/chats`), no supervisor, no agent (`docs/ARCHITECTURE.md` §Process model). See `.rules/agent-registry.md`, `.rules/user-settings.md`, `.rules/chat-registry.md` for the surfaces.

| Concern | Owner |
|---|---|
| Composition: settings, store, schema, refusals, engine, routers, health routes | `multiuser_config/config_registry/app.py` |
| Binding and serving | `multiuser_config/config_registry/__main__.py` |
| The routers both roots include | `config_registry/*/api/http/endpoint.py` |
| The refusal set every root runs | `ouroboros/app/multiuser_validation.py` |
| Shared start-up steps this root repeats | `multiuser_config/bootstrap.py` |

**Rules:**

- Nothing here imports what executes work (`supervisor`, `ouroboros.agent_runtime`, `ouroboros.agent`) — test parses import graph.
- Composes inline, does not use `bootstrap_process` (which refuses a process with no model provider; this tier never calls a model).
- `TIER` names the root, not a surface — it is what the start-up line prints and what the deployment keys the tier by.
- Every refusal is the shared one; this root adds none.
- One FastAPI application, its routers, one engine. The trace middleware is installed once.
- No owner decision here — the shared resolver in `infra/utils/http/fastapi.py` reads the mode per request.
- Publish on `app.state` only what a mounted route reads, and nothing owner-bearing. The probe holds its own connection.
- Bind the configured address or exit non-zero. No free-port scan, no port file, no killing whoever holds a port.
- One root per process.
