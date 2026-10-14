---
paths:
  - "config_registry/user_settings/**"
  - "tests/user_settings/**"
---

# Settings an owner may change — read before editing

Owner-changeable keys per owner+agent, plus personal MCP tokens. Every key is keyed by `(owner_id, agent_id, key)`.

| Concern | Owner |
|---|---|
| Boundaries: reader's sync store, async face, writer's repo, sync command face | `config_registry/user_settings/interfaces.py` |
| Statements over both engines | `config_registry/user_settings/infra/repositories/repo.py` |
| Entry key the reader and write surface meet on | `config_registry/user_settings/infra/cache.py` |
| Settings object every reader imports | `config_registry/user_settings/reader.py` |
| HTTP router: settings keys + personal MCP token routes | `config_registry/user_settings/api/http/endpoint.py` |
| Personal token payload models, listing builder | `config_registry/user_settings/api/http/mcp_servers.py` |
| Personal token request model, TTL/length bounds | `config_registry/user_settings/api/http/personal_tokens.py` |
| Token writer those routes call | `infra/user_tokens/` — see `.rules/infra.md` |
| Which servers take a personal token | `ouroboros/app/egress_auth_composition.py` (`EgressAuthComposition.personal_token_targets`) |
| Owner resolver, `is_configured_owner`, trace middleware | `infra/utils/http/fastapi.py` |
| Table | `infra/migrations/` (owner settings + agent re-keying) |
| Tests | `tests/user_settings/`, `tests/infra/test_cache.py` |

**Rules:**

- Each accessor owns its key and handles its value itself — trimmed text, or the instance value when absent. No key map, no shared parser. The reuse is exactly one private method: `UserSettings._override(scope, key)`.
- Every key has a synchronous accessor and an `async_`-prefixed twin resolving the same way over the async store and `IAsyncCache` — a caller on a loop may not read a database on it. A new key adds both or neither; `background_memory` is the caller with no thread, and its guard fails a coroutine that calls a synchronous accessor.
- Boolean keys (`true`, `false`, `1`, `0`) and skill allowlists raise on malformed values for both callers; MCP selection resolves malformed to deny-all.
- No process-lifetime cache wherever the writer is another process. `inmemory` cache is refused in component tiers; `redis` is invalidated by the writer on every `PUT`/`DELETE`; `none` is the default.
- The cache buys repetition, not breadth: a cold scope still costs one statement per key. Every entry expires (`OUROBOROS_SETTINGS_CACHE_TTL_SEC`).
- The read degrades, the invalidation refuses: `get_value`/`set_value` swallow an unreachable cache client; `delete_value` raises (a committed row reporting success while readers keep the old value).
- A `PUT`/`DELETE` drops exactly the key it changed, never the scope.
- The reader's synchronous face takes `ICache`, its async twin and the writer take `IAsyncCache`. On `inmemory` the faces are one set of entries.
- `CacheFactory` is initialized once by a composition root; nothing composes one on the way past. The settings object is not a root's to compose — `user_settings_factory.settings()` builds it on first use.
- Every statement is keyed on `(owner_id, agent_id)`, written inside the method. No module-level statement constants.
- This package imports no transport, no `supervisor`, no `ouroboros.agent`, no `multiuser_config` (test parses import graph).
- The write repository only writes (`IUserSettingsRepo`); nothing reads through it.
- `/reasoning on|off` uses `ISyncUserSettingsRepo` on the supervisor thread (no event loop there); it writes that row's upsert at its own `execute`, as the HTTP route does, and a third writer of it is the defect.
- The router holds its write repositories at import over the engine holder; each route receives the registry check through `Depends`.
- The surface reaches exactly `OWNER_SETTING_KEYS`, pinned by a test. It stores text; the reader makes sense of it.
- A token is never echoed, logged, or cached. The `PUT` body is parsed by hand (FastAPI's 422 renders input). `token` stays an unconstrained `str`. Payload models live outside `endpoint.py` (guard test rejects `transport` there).
- `PUT` refuses the configured owner and a server that takes no token; `DELETE` refuses neither (a token stays revocable whatever the list says now).
