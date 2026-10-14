---
paths:
  - "ouroboros/mcp/**"
  - "ouroboros/sandbox/**"
---

# MCP client and skill sandbox — read before editing

| Concern | Owner |
|---|---|
| One MCP server: connection and the tools it publishes | `ouroboros/mcp/connection.py` |
| Process-wide manager, initialised by whichever root composes tools | `ouroboros/mcp/manager.py` |
| Client transports, write-stream credential wrapper, cross-origin hook | `ouroboros/mcp/transports.py` |
| Authorization kinds → authenticators, `EgressAuthDependencies` | `ouroboros/egress/auth/` (`docs/ARCHITECTURE.md` §Security context and per-call MCP credentials) |
| Root-level dependency composition: which user token store, key refusal, undeclared forwarded names | `ouroboros/app/egress_auth_composition.py` |
| Cross-loop scheduling, shared with A2A | `new_core/tools/loop_bridge.py` |
| Catalog entry point for MCP tools | `McpToolProvider` (`docs/ARCHITECTURE.md` §Tool sources: one provider interface, one catalog) |
| Sandbox runner and the soft profile | `ouroboros/sandbox/runner.py`, `ouroboros/sandbox/soft.py` |
| Skill subprocess execution | `ouroboros/tools/skill_exec.py` — see `.rules/skills.md` |

**Rules:**

- Per-call credentials ride only on the request of one tool call — no ambient carrier. If an SDK bump moves the write off the awaiting task, credentials silently stop reaching the wire (`tests/ouroboros/mcp/test_mcp_user_credentials.py`).
- The stores are a dependency of initialization (`EgressAuthDependencies`), not of a call. A new kind that needs a new dependency adds a field there, not a parameter at four seams.
- An authenticator loads only what its kind declares. A missing value is refused by name; nothing saved is the same refusal.
- `"Credentialed" = forwarded_headers is non-empty` — never derive from `auth.kind` (`user_token` over `sse` forwards nothing).
- A user token is personal, then static, then refused — never a fallback on failure.
- A credentialed connection keeps no cookies, strips credentials off-origin, and never lists from inside a call.
- Per-call credentials are streamable-HTTP only. The schema refuses `user_headers` over `sse`; `user_token` over `sse` maps to `NoAuthenticator`.
- Credentials never use plaintext HTTP (`user_headers`, `user_token` refuse an http URL; `none` and `mtls` may use one).
- `ouroboros/egress/auth/` imports no transport; `ouroboros/storage/settings/schema.py` imports nothing from `ouroboros.mcp`.
- The MCP server list is not a setting — it comes from `security_config/mcp.py`, imported once at startup. `MCP_ENABLED` is a kill switch above it.
- `OUROBOROS_USER_ENABLED_MCP_IDS` is per owner+agent; absence falls back to instance env (`[]` default), `[]` denies all.
- `_coerce_mcp_servers` names each entry it drops — never make that path quiet.
- Three budgets are settings (`MCP_TOOL_TIMEOUT_SEC`, `MCP_REQUEST_TIMEOUT_SEC`, `MCP_STREAM_READ_TIMEOUT_SEC`); everything else is a constant. The connect budget is derived, not exposed — do not write it back as a literal.
- The protocol era is negotiated per connection; branches read the era, never the transport. Both transports reach both eras. Do not delete either session-death detector as redundant.
- A gate that selects tools by origin must name every remote source it covers (`ext:`, `mcp:`, `a2a:`).
- Skill subprocess confinement stays in `skill_exec.py` and this package — do not grow a second isolation path. Widening the subprocess environment is a security change.
