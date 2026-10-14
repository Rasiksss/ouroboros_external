---
paths:
  - "ouroboros/ingress/**"
  - "ouroboros/egress/**"
---

# Ingress and egress packages — read before editing

Mirrored directions between this agent and other systems (`docs/ARCHITECTURE.md` §Ingress, §Egress). The user-facing transports live under `transport/` — see `.rules/transport-seam.md`.

| Concern | Owner |
|---|---|
| SberChat-specific normalization boundary (package stub today) | `ouroboros/ingress/sberchat/` |
| Per-agent `httpx` client, SDK client, `A2A-Version` header | `ouroboros/egress/a2a/transports.py` |
| One remote agent: card discovery, published tool, task lifecycle | `ouroboros/egress/a2a/connection.py` |
| Process-wide singleton the lifespan initialises | `ouroboros/egress/a2a/manager.py` |
| A2A auth dependencies and task handles | `ouroboros/egress/a2a/dependencies.py`, `ouroboros/egress/a2a/task_handles.py` |
| WorkStation sandbox client (connect, code_exec, file_read/write/edit, apply_patch) | `ouroboros/egress/workstation/` |
| Cross-loop scheduling, shared with MCP | `new_core/tools/loop_bridge.py` |
| Remote agent list (SSOT, not settings) | `security_config/a2a.py` — see `.rules/settings.md` |

**Rules:**

- One tool per agent, never one per skill. Follow-up is served by `a2a_task_status` and `a2a_task_cancel` (N agents = N+2 tool names).
- The deadlines nest and the innermost one must fire — only it knows the `task_id`.
- A send with no answer returns `⚠️ A2A_SUBMISSION_UNKNOWN`, classified with the media `*_DELIVERY_UNKNOWN` codes so the repeat guard blocks an identical retry on sight.
- An interrupted task is a result, not a failure — `⚠️ A2A_INPUT_REQUIRED` is deliberately excluded from `_FAILURE_PREFIXES`.
- A per-agent connection failure at startup is logged and skipped, never aborts the rest or the server. The agent list carries credentials and is never projected into a subprocess environment.
