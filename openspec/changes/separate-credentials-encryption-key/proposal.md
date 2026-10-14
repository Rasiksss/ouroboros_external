## Why

`OUROBOROS_CHAT_ENCRYPTION_KEY` does two unrelated jobs: it optionally encrypts message fields, and it always
seals the credentials outbound calls carry — `security_contexts`, `mcp_user_tokens`, `a2a_user_tokens` — and
keys the A2A task handles. One secret, two purposes and two threat models; and because of the second job an
optional chat setting is mandatory for every root. Credentials get their own key and a cipher with a 512-bit
key.

## What Changes

- **BREAKING** New mandatory setting `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`: 64 bytes of base64, whitespace
  ignored. The server, the browser transport tier and the agent tier refuse to start without it; the
  personal-token routes of the config registry answer 503 naming it. No fallback to the chat key.
- Security context values and personal MCP and A2A tokens are sealed with AES-256-SIV (RFC 5297) under the
  new key, used directly, with a random 16-byte nonce as the last associated-data component. The associated
  data of each table is unchanged.
- The A2A task-handle key is derived from the new key; handle format and domain label are unchanged.
- **BREAKING** No migration: rows sealed under the chat key no longer open, and open task handles no longer
  verify. The tables exist only on develop and the dev stand, which are cleaned by hand.
- `OUROBOROS_CHAT_ENCRYPTION_KEY` serves message field encryption alone and is no longer needed to start.
  Every root validates chat field encryption at start, so an enabled encryption with an unusable key still
  refuses a production start instead of failing on the first message.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `security-context`: the SQL backend and every root that installs it take the credentials key; values are
  AES-256-SIV ciphertext; a value sealed under the chat key does not open.
- `user-token-store`: personal tokens are sealed with AES-256-SIV under the credentials key; the scenario that
  kept pre-`0019` MCP rows opening is withdrawn; roots refuse naming the credentials key.
- `a2a-egress`: task handles are keyed from the credentials key, validated at composition.
- `agent-runtime-process`: the agent tier composes its stores and task handles from the credentials key.
- `multiuser-mode`: tiers share the credentials key, which is what lets a handle open on every replica.

## Impact

- New module `infra/credentials_key.py`; `infra/security_context/sql.py`, `infra/user_tokens/sql.py`,
  `ouroboros/egress/a2a/task_handles.py`, `ouroboros/app/egress_auth_composition.py`,
  `config_registry/user_settings/api/http/endpoint.py`, `ouroboros/storage/settings/schema.py`,
  `ouroboros/storage/message_field_crypto.py`, and the roots `server.py`, `multiuser_config/bootstrap.py`,
  `multiuser_config/config_registry/app.py`, `multiuser_config/transport/browser/app.py`,
  `multiuser_config/agent_runtime/run.py`.
- `docs/SETTINGS.md`, `docs/INVARIANTS.md` (#8), `.rules/infra.md`, `docker/docker-compose.yml`,
  `security_config/{mcp,a2a}.py`.
- Operations: every tier needs the same new secret; the dev stand deletes the rows of the three tables.
- No new dependency: `AESSIV` ships with the `cryptography>=42.0` already required. No schema migration.
