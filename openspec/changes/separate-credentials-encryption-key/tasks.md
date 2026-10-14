## 1. Failing tests first

- [x] 1.1 `tests/infra/test_credentials_key.py`: round trip; two seals differ; format pin with bare `AESSIV`; foreign AAD or key raises; empty, non-base64, 16-, 32- and 48-byte keys refused naming the setting without the value; a 64-byte key over two lines accepted; a chat-key AES-GCM value does not open; `derive` is HMAC-SHA256
- [x] 1.2 `validate_chat_encryption()` refuses in production, warns in development, does nothing when disabled; every root in `_ROOTS` calls it after `validate_production_storage` (shared AST helper moved to `tests/helpers/`)
- [x] 1.3 Key separation: the real `server.lifespan` starts with an empty chat key and a valid credentials key; the transport and agent tiers do the same and refuse a 32-byte credentials key naming the new setting; `server.py` passes `settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` at all three call sites
- [x] 1.4 Run the new tests against the unchanged code and record that they fail

## 2. Credentials key

- [x] 2.1 Add `infra/credentials_key.py` with `CredentialsKey` (`seal`, `unseal`, `derive`, `_decoded`)
- [x] 2.2 Seal `security_contexts` through it in `infra/security_context/sql.py`
- [x] 2.3 Seal both token tables through it in `infra/user_tokens/sql.py`; remove `_sealing_key`
- [x] 2.4 Derive the task-handle key through it in `ouroboros/egress/a2a/task_handles.py`
- [x] 2.5 Name the new setting and 64 bytes in `ouroboros/app/egress_auth_composition.py` and in the 503 of `config_registry/user_settings/api/http/endpoint.py`, which reads the new setting
- [x] 2.6 Make `decode_encryption_key`, `seal` and `unseal` of `ouroboros/storage/message_field_crypto.py` private, without associated data (review follow-up); old-format test rows come from `sealed_under_the_chat_key`

## 3. Settings and roots

- [x] 3.1 Declare `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` in `AppSettings`, in `to_persist_dict()` and in `_coerce_secret_fields`
- [x] 3.2 Pass it in `server.py`, `multiuser_config/transport/browser/app.py` and `multiuser_config/agent_runtime/run.py`
- [x] 3.3 Add `validate_chat_encryption()` to `ouroboros/storage/message_field_crypto.py` and call it after `validate_production_storage()` in `server.py`, `multiuser_config/bootstrap.py`, `multiuser_config/config_registry/app.py` and `multiuser_config/transport/browser/app.py`

## 4. Existing tests

- [x] 4.1 `TEST_ENCRYPTION_KEY` → 64-byte `TEST_CREDENTIALS_KEY` and its importers; the transport tier's autouse key fixture
- [x] 4.2 File-local and inline credential keys to 64 bytes; bad-key cases gain a valid 32-byte key; assertions name the new setting and 64 bytes
- [x] 4.3 `seed_user_token` seals through `CredentialsKey`; the pre-`0019` MCP row test becomes the chat-key row test
- [x] 4.4 Governance fragment for invariant 8; explicit pin of the new secret in `test_settings_schema.py`

## 5. Documentation and configuration

- [x] 5.1 `docs/SETTINGS.md`: chat key row, new credentials key row, sections 3, 4, 5 and 6.2
- [x] 5.2 `docs/INVARIANTS.md` invariant 8 and `.rules/infra.md` (owner row, lines on the cipher and the shared sealing helper)
- [x] 5.3 `security_config/mcp.py` and `security_config/a2a.py`: drop the sentences restating the key
- [x] 5.4 `docker/docker-compose.yml`: guard and hint move to the new key

## 6. Verification

- [x] 6.1 `ruff format`, `ruff check`, `mypy`
- [x] 6.2 Focused suites for every touched test file, then `./scripts/check.sh`
- [x] 6.3 Independent review of the diff in a clean context against `docs/INVARIANTS.md` and §Coding Rules; fixes re-reviewed
- [x] 6.4 Decisions taken without asking appended to `HANDOFF.md`
