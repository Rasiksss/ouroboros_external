---
paths:
  - "ouroboros/storage/settings/**"
  - "ouroboros/app/config.py"
  - "security_config/**"
---

# Settings and defaults — read before editing

| Concern | Owner |
|---|---|
| `AppSettings` schema | `ouroboros/storage/settings/schema.py` |
| Owner overrides | `config_registry/user_settings/` (`.rules/user-settings.md`) |
| Runtime settings/path readers | `ouroboros/storage/settings/store.py`, `ouroboros/storage/settings/paths.py` |
| Bootstrap: materializes `AppSettings` once at startup | `ouroboros/storage/settings/bootstrap.py` |
| Startup config wiring | `ouroboros/app/config.py`, `server.py` |
| MCP server list | `security_config/mcp.py` |
| A2A remote-agent list | `security_config/a2a.py` |
| Deployment prompt | `security_config/PROMPT.md`, read by `ouroboros/agent/context.py` |

**Rules:**

- Production config is **environment-only**, with one exception: the keys in `OWNER_SETTING_KEYS`, set/cleared per agent over `/api/owner-settings`. Do not reintroduce `/api/settings` or a persistent settings file.
- Known `AppSettings` fields use attributes; extras go through an explicit API such as `extra_fields()`.
- Both security-config lists name every entry they drop, and neither renders the entry (`describe_validation_error` keeps `auth_token` out of the log). An empty list is not an error — it skips MCP manager initialization and registers no A2A egress.
- Three MCP budgets are settings (`MCP_TOOL_TIMEOUT_SEC`, `MCP_REQUEST_TIMEOUT_SEC`, `MCP_STREAM_READ_TIMEOUT_SEC`); the rest are code. The defaults live in `AppSettings` and only there — `ouroboros/mcp/` keeps no constant of its own. The connect budget is derived, not exposed.
- The MCP and A2A lists are not settings — imported once at startup from `security_config/`, branch on `OUROBOROS_POLIGON`. `MCP_SERVERS` and `A2A_REMOTE_AGENTS` stay confined to `security_config/` and `schema.py` (`tests_headless/test_security_config_ssot.py`).
