---
paths:
  - "transport/browser/**"
  - "ouroboros/gateway/**"
---

# HTTP / WS payloads — read before editing

| Concern | Owner |
|---|---|
| Payload shapes (TypedDicts for `/api/health` and other gateway payloads) | `transport/browser/contracts.py` |
| Routes and the default health surface | `ouroboros/gateway/router.py` |
| Mounted app state | `ouroboros/gateway/state.py` |
| Parity tests | `tests/ouroboros/gateway/test_gateway_parity.py`, `tests_headless/test_headless_runtime.py` |
| Contract tests | `tests/ouroboros/contracts/test_contracts.py` |
| Recording the caller's security context at the WebSocket handshake | `transport/browser/ws.py`; collector and store are `infra/security_context/` — see `.rules/infra.md` |

**Rules:** a payload change updates the contract, every adapter that builds it, and the parity tests together. Keep business logic out of routers. New *browser* routes need an explicit product decision — headless production keeps the minimal documented set (`docs/ARCHITECTURE.md`).

Security context recording happens at the handshake, after `scope_for` succeeds and before `accept()`: `store.save(scope.security_token, collector.collect(headers.items()))`. The token is read off the resolved scope, never from a header. The endpoint collects nothing itself — what is collected is the image's declaration (`security_config/auth/header_collector.yaml`). A refused handshake records nothing; a failed collection or save refuses the handshake (1011). No HTTP route records. A test that needs an uninstalled holder replaces this module's `security_context` or `security_context_collector` reference; the holders have no reset.
