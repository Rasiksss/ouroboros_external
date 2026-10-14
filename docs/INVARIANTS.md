# Key invariants (headless)

Properties of the headless runtime. **Violating one is a bug even if tests pass.**

An invariant is a property that (1) is not obvious from reading the code, (2) breaks silently
when violated — tests pass but the system is wrong, and (3) whose violation causes a security,
data-loss, or correctness failure. If it does not meet all three, it belongs in `ARCHITECTURE.md`,
`DEVELOPMENT.md`, or `.rules/*.md`, not here.

**Code owners:** changes to this file require approval from **Anton L.** or **duxx**.
A merge without their review is not authorized, however green the pipeline.

1. **No development/self-improvement docs in task context:** `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, checklists, improvement backlog, and execution reflections are not injected into normal LLM task context (`ouroboros/agent/context.py`).
2. **No self-mod tools** in the production tool catalog — no `repo_write`, `repo_commit`, `run_shell`, or git tools.
3. **Minimal HTTP attack surface:** no settings editor or WebSocket UI on the default headless router; browser ingress is opt-in (`BROWSER_INGRESS_ENABLED=true`). Three owner-bearing route groups are admitted: `/api/agents`, `/api/owner-settings` (the keys in `OWNER_SETTING_KEYS`, per agent, plus write-only personal MCP tokens), and `/api/chats` — all read the owner from `X-Ouroboros-Owner-Id` through the one resolver in `infra/utils/http/fastapi.py`. A further group needs the same explicit product decision.
4. **Core import boundary:** `new_core/graph`, `new_core/loop`, `new_core/llm`, `new_core/prompt`, and `new_core/skills` import no application package; every external effect goes through an injected interface. `new_core/tools` is the single documented migration exemption.
5. **One task path:** production runs the task through `build_task_graph` / `invoke`. There is no second loop entry point, no `agent/loop.py`, and no per-task `ToolCatalog` — one catalog per process, with `ToolContext` passed as an argument rather than held.
6. **Two headers on every hop:** `X-Trace-Id` (which operation) and `X-Agent-Id` (which agent) travel on every request this process makes, per request rather than merged in at construction. Every transport ingress establishes `X-Trace-Id` explicitly — accepting the caller's when its protocol carries one, originating when it does not. `X-Agent-Id` comes from `AGENT_SYSTEM_CONFIGURATION_AGENT_ID` (or `AGENT_ID`); unconfigured means no header, never an empty one. Work that leaves the originating thread carries the identifier as data on the item handed over (`MemoryEvent.trace_id`, `InboundMessage.trace_id`, `OutboundMessage.trace_id`); a context variable is never a carrier between two. A process-lifetime worker originates one per unit of work. An integration that rejects `X-Agent-Id` is used without it, once and on the record.
7. **OwnerScope minted once by the transport:** `owner_id`, `agent_id`, `surface_id`, and `chat_id` are set once at the transport boundary and travel unchanged through the entire pipeline. No layer re-derives, overwrites, or accepts them from message payload. A second construction site is a cross-owner data leak.
8. **Encryption key required:** `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, exactly 64 bytes of base64, is mandatory for every root that installs the security context store, the personal token store or the A2A task handles, and never falls back to `OUROBOROS_CHAT_ENCRYPTION_KEY`. `AESSIV` itself accepts 32- and 48-byte keys, so relaxing the length check silently seals credentials under a weaker AES-SIV; making the key optional stores security context values and personal tokens unsealed.
9. **Drain before shutdown:** the runtime must wait for in-flight tasks to finish (bounded by `OUROBOROS_DRAIN_TIMEOUT_SEC`) before exiting. A process that exits while a task is still writing its outcome loses data silently — the task body may have completed but its result was never persisted.

---

Owning docs: `docs/ARCHITECTURE.md` for structure and data flow, `docs/DEVELOPMENT.md` for
engineering policy. Changing an invariant is a structural decision, not a cleanup.
