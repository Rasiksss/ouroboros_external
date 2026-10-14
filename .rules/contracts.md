---
paths:
  - "ouroboros/contracts/**"
---

# Frozen contracts — read before editing

`ouroboros/contracts/` pins the shapes that cross module boundaries, so a change here is a change
to every caller at once (`docs/ARCHITECTURE.md` §Frozen contracts).

| Concern | Owner |
|---|---|
| Skill payload policy | `skill_payload_policy.py`; manifest shape lives in `new_core/skills/manifest.py` |
| `ToolContextProtocol`, including the `events` channel a tool reports through | `tool_context.py`, `tool_abi.py` |
| `PluginAPI` | `plugin_api.py` |
| Chat id policy | `chat_id_policy.py` |
| Runtime command vocabulary — shared because a transport may not import the supervisor | `chat_commands.py` |
| Owner scope | `owner_scope.py` |
| Wire helpers both seam codecs share — unknown keys, declared types, JSON safety | `wire.py` |
| The outage policy every broker backend's readers share (`BrokerOutage`), and the Redis stream reply shapes | `stream_replies.py` |
| Sandbox policy, media delivery, schema versions, API v1 | `sandbox_policy.py`, `media_delivery.py`, `schema_versions.py`, `api_v1.py` |

**Rules:** extend only with tests in `tests/ouroboros/contracts/test_contracts.py`. Browser TypedDicts are *not* here —
they live in `transport/browser/contracts.py` (`.rules/http-contracts.md`). `chat_commands.py` is
the single source of the command vocabulary for the Dialogs addressing gate, intent classification,
the browser operator-command gate, and `supervisor/commands.py` dispatch; adding a command at one of
those call sites instead creates a second SSOT. **Removing a command means moving it to
`RETIRED_SLASH_COMMANDS`, not deleting the name**: dropped from the vocabulary altogether it becomes
work for the agent, which answers a command it cannot run as though it had run.
