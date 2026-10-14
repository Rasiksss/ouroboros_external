---
paths:
  - "multiuser_config/transport/**"
  - "multiuser_config/*.py"
---

# The browser transport as its own process — read before editing

The second composition root. Composes a transport tier and nothing else (`docs/ARCHITECTURE.md` §Process model).

| Concern | Owner |
|---|---|
| Composition: settings, store, schema, refusals, identity, gateway, routes | `multiuser_config/transport/browser/app.py` |
| Serving | `multiuser_config/transport/browser/__main__.py` |
| Address and serving every tier goes through | `multiuser_config/bootstrap.py` |
| Refusal set every root runs | `ouroboros/app/multiuser_validation.py` |
| Routes both roots mount | `ouroboros/gateway/router.py`, `transport/browser/router.py` |

**Rules:**

- Nothing in this package imports `supervisor` or `ouroboros.agent*` — test parses import graph. The rule is this tier's, not `multiuser_config/`'s.
- `transport/browser` is never forked — both roots mount the same route collection and hub.
- Every refusal is the shared one. This root adds no check of its own.
- Publish on `app.state` only what a mounted route reads. `runtime_status`, `supervisor_ready_event` and `get_supervisor_error` stay unpublished — their answers are the supervisor tier's.
- Serve the configured address or exit non-zero. No free-port scan, no port file, no killing whoever holds a port.
- One root per process.
