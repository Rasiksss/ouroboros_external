## Context

`ouroboros/storage/message_field_crypto.py` implements optional AES-256-GCM encryption of message fields
under `OUROBOROS_CHAT_ENCRYPTION_KEY`, gated by `OUROBOROS_CHAT_ENCRYPTION_ENABLED` and evaluated lazily on the
first encrypted read or write. Its `decode_encryption_key`, `seal` and `unseal` were reused for a second,
unconditional job: `SqlSecurityContextStore`, `SqlUserTokenStore`/`SqlUserTokenRepo` and `A2aTaskHandles`
decode the same key at construction, so the server, the browser transport tier and the agent tier refuse to
start without it, and the config registry answers 503 on the personal-token routes.

Two consequences follow. The chat key protects two kinds of data with different threat models and cannot be
rotated for one without the other. And the production refusal of an enabled chat encryption with an unusable
key, which `docs/SETTINGS.md` §2.9 promises at start, only happens at start because those credential stores
decode the key; the chat path itself is lazy.

## Goals / Non-Goals

**Goals:**
- One key, `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, for every credential sealed at rest and for the task-handle
  key; the chat key serves message fields alone.
- A cipher whose key is 512 bits.
- Every root still refuses an enabled chat encryption with an unusable key before it serves.

**Non-Goals:**
- Re-sealing existing rows, dual reads, or a fallback to the chat key.
- Key rotation, key ids, or a version prefix in the ciphertext.
- Changing message field encryption, its format or its key.
- Changing which settings a skill manifest may request (`FORBIDDEN_SKILL_SETTINGS`).

## Decisions

**AES-256-SIV with the 64-byte key used directly.** There is no AES-512: AES keys are 128, 192 or 256 bits.
RFC 5297 AES-SIV takes a double-length key, so AES-256-SIV is the standard AES mode keyed by 512 bits: 256 for
S2V (CMAC) and 256 for CTR. It is an AEAD, misuse-resistant (a repeated nonce leaks only equality), and
`cryptography` ships it as `AESSIV` in every version `>=42.0` the project already requires. Considered: a
512-bit master key with HKDF-derived AES-256-GCM subkeys (the cipher stays 256-bit, "512" would describe
only key material), and A256CBC-HS512 from JOSE (no primitive in `cryptography`; hand-assembled CBC, padding
and MAC). HKDF separation of the sealing key and the handle key, and a version prefix, were offered and
declined by the owner: the configured key is the SIV key itself.

**Randomized, with the nonce last.** Each seal draws a random 16-byte nonce and passes the associated-data
vector `[json_aad, nonce]` — RFC 5297 §3 places the nonce as the final component. The stored text is
`urlsafe_b64(nonce ‖ SIV ‖ ciphertext)`. Randomization keeps the existing guarantee that two saves of one
value store different ciphertexts. The associated data of every table is unchanged, so the binding of a
ciphertext to its token, type and name, or to its target, owner and agent, holds as before. `AESSIV` requires
the vector to be a `list`; a tuple raises `TypeError`, which the type stubs do not catch.

**Exactly 64 bytes, whitespace ignored.** `AESSIV` itself accepts 32-, 48- and 64-byte keys, so a 32-byte
chat key pasted into the new setting would silently run AES-128-SIV. The decoder removes every whitespace
character, decodes strict base64 and accepts only 64 bytes. Removing whitespace is deliberate:
`openssl rand -base64 64` wraps its output across two lines, and a Vault secret file is only `strip()`-ped by
`ouroboros/storage/settings/env_files.py`. A refusal names the setting and never renders or chains the value.

**One class, `infra/credentials_key.py`.** `CredentialsKey(encryption_key: SecretStr)` decodes once and
offers `seal`, `unseal` and `derive(label)` (HMAC-SHA256, the task-handle derivation unchanged but for the
key). The stores and the task handles keep their constructor signatures and build it internally, so each
root still hands them the setting and the refusal still happens at composition, before any connection.
Their per-class refusal texts give way to the one text of `CredentialsKey`; the wrappers that add context —
`EgressAuthCompositionError` and the 503 of the owner-settings routes — keep theirs. It lives in `infra/`
beside its two sealing consumers; `infra/user_tokens/sql.py` loses `_sealing_key`, which would be an empty
wrapper. This is not a second implementation of message field encryption, which
`background-memory-events` forbids: it is a different primitive under a different secret, and
`message_field_crypto` stays the only implementation of field encryption.

**The chat-key helpers become private.** With the credentials moved, `decode_encryption_key`, `seal` and
`unseal` of `message_field_crypto` had no caller outside the module. They are now private and take no
associated data, so the chat key's AES-GCM path cannot be picked up again to seal credentials; tests that
fake rows of the old format build them with bare `AESGCM` (`tests/helpers/security_context.py`).

**Chat encryption is validated at start by every root.** `message_field_crypto` gains
`validate_chat_encryption()`, which evaluates the existing configuration: in `production` an enabled
encryption with an unusable key raises `FieldKeyUnavailable`, in `development` it warns and disables, and a
disabled encryption does nothing. Every root calls it right after `validate_production_storage()` — the
server's lifespan, `bootstrap_process` (agent, supervisor and background-memory tiers), the config registry
and the browser transport tier. It is the shared refusal every root runs, not a root's own check. Considered:
calling it only in the three roots that decoded the chat key before (keeps today's accidental coverage), or
documenting a failure on first use (weakens §2.9).

**No migration.** The three tables exist only on develop and the dev stand. Rows sealed under the chat key
raise on load — a `user_token` call is then refused rather than falling back to the static token — until the
rows are deleted by hand. The `user-token-store` scenario that kept pre-`0019` MCP rows opening becomes false
and is withdrawn by restating its requirement under a new name.

## Risks / Trade-offs

- [Rows sealed under the chat key fail every load that selects them] → delete the rows of
  `security_contexts`, `mcp_user_tokens` and `a2a_user_tokens` when deploying; security contexts are
  collected again at the next browser connection, owners set personal tokens again.
- [Open A2A task handles stop verifying] → finish or abandon long delegations before deploying.
- [Tiers on different keys cannot read each other's rows or handles] → one secret value for the server,
  transport, agent and config-registry tiers, deployed together.
- [The same 64-byte key keys AES-SIV and the HMAC that derives the handle key] → accepted by the owner; no
  known interaction between HMAC-SHA256 and AES-CMAC/CTR under one key. HKDF separation stays possible.
- [No version prefix: a future rotation cannot tell formats apart] → accepted; a rotation will need its own
  change.
- [An image whose `cryptography` build lacks AES-SIV] → `CredentialsKey` is built at composition, so such an
  image refuses to start instead of failing on the first call.

## Migration Plan

1. Generate one key: `openssl rand -base64 64 | tr -d '\n'`; set `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` in
   every tier.
2. Deploy all tiers together; run
   `DELETE FROM security_contexts; DELETE FROM mcp_user_tokens; DELETE FROM a2a_user_tokens;`.
3. Keep `OUROBOROS_CHAT_ENCRYPTION_KEY` only where message field encryption is enabled.

Rollback is the previous image with the chat key; rows written under the credentials key then fail the
same way and are deleted the same way.
