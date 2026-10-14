---
paths:
  - "multiuser_config/agent_runtime/**"
  - "multiuser_config/*.py"
---

# The agent runtime tier as its own process — read before editing

The fourth composition root. Claims work and runs tasks; composes no supervisor and no owner-facing surface (`docs/ARCHITECTURE.md` §Process model). What executes a task is `ouroboros/agent_runtime/` — see `.rules/agent-loop.md` and `.rules/supervisor.md`.

| Concern | Owner |
|---|---|
| Composition: tool-client loop, budget, catalogue, runtime, exit | `multiuser_config/agent_runtime/run.py` |
| One substitution point: agent events, outbound, work signal | `multiuser_config/agent_runtime/dependencies.py` |
| Event loop this process owns for coroutine tool clients | `multiuser_config/agent_runtime/tool_client_loop.py` |
| Shared start-up, address, probe server | `multiuser_config/bootstrap.py` |
| Refusal set every root runs | `ouroboros/app/multiuser_validation.py` |
| Broker backend selector | `ouroboros/app/broker_composition.py` |
| MCP and A2A clients this root initialises | `ouroboros/mcp/manager.py`, `ouroboros/egress/a2a/manager.py` |

**Rules:**

- Every refusal is the shared one, made by `bootstrap_process`. This root adds no check of its own.
- The memory-event publisher is installed before `compose_process` — the readers that snapshot the holder are composed there.
- This process owns an event loop for MCP and A2A clients (`tool_client_loop.py`); started before the catalogue, stopped after the runs drain, never reached as a global.
- Connecting is part of composing: pass servers and loop to the provider's constructor — one path to `initialize()`, not two.
- A kill switch gates the connect, not the descriptors: `MCP_ENABLED` / `A2A_EGRESS_ENABLED` off means the provider is handed no remote entries at all.
- The exit runs whole: drain, then close both managers within one shared budget, then stop the loop — each step guarded, every wait bounded.
- The probes serve the loop's heartbeat and the probe's own connection, and nothing else. Liveness reads the claim loop at `CLAIM_LOOP_STALL_SEC`.
- A tool source that cannot connect does not fail the start — logged and skipped.
- One root per process.
