---
paths:
  - "infra/**"
  - "tests/infra/**"
---

# Shared infrastructure — read before editing

The lowest layer: what more than one package needs to reach the same external service the same way.

| Concern | Owner |
|---|---|
| Async engine beside the synchronous store | `infra/async_engine.py` |
| Failover across configured PostgreSQL URLs | `infra/async_failover.py` |
| Redis client, synchronous and asyncio: timeouts, retry, Sentinel, key prefix | `infra/redis_client.py` |
| Kafka client for both libraries: connection, factory, topic/group helpers, kafka-python and aiokafka | `infra/kafka_client.py` |
| Cache boundaries: `ICache` and `IAsyncCache` | `infra/cache/interface.py` |
| Cache backends: none, in-process, shared over Redis | `infra/cache/null.py`, `infra/cache/inmemory.py`, `infra/cache/redis_cache.py` |
| Cache configuration: tier refusal, sub-second budget, `CacheFactory` | `infra/cache/__init__.py` |
| Owner resolver and trace middleware | `infra/utils/http/fastapi.py` |
| Security context: types, store, collector, holders | `infra/security_context/` (`docs/ARCHITECTURE.md` §Security context and per-call MCP credentials) |
| Declaration models and the shipped declaration | `infra/security_context/declaration.py`, `security_config/auth/` |
| Personal MCP tokens: `IUserTokenStore` (load-only) and writer `IUserTokenRepo` | `infra/user_tokens/` |
| Credentials sealing key: AES-256-SIV for context values and personal tokens, the A2A task-handle key | `infra/credentials_key.py` |
| Tables, and the Alembic tree every tier's start-up reads its head from | `infra/migrations/` — see `.rules/persistent-data.md` |
| Tests | `tests/infra/`, `tests/ouroboros/app/test_broker_composition.py` |

**Rules:**

- Two client libraries behind one Kafka description: kafka-python builds every producer, the synchronous group consumer and the partition reader; aiokafka builds `async_group_consumer`, which only background memory reads with. `_async_common_kwargs` translates the same connection — a second description is the defect, a second binding of one is not.
- The asynchronous reader has its own error tuple (`aiokafka_unanswered_errors`, without `ConsumerStoppedError` — reading a consumer nobody started is a programming error) and joins and leaves a group under `KAFKA_JOIN_TIMEOUT_SEC`, because `start` otherwise waits for topic metadata inside a request timeout a long poll interval makes minutes long.
- `refuse_unusable_poll_interval` is the one refusal both factory paths and the Kafka double call: a double that admits what production refuses hides the divergence until there is a real broker.
- One description per external service. `broker_composition.py` and the settings cache both compose a Redis client from `infra/redis_client.py`; a second factory would duplicate timeouts/policy silently.
- `redis_client` and `async_redis_client` share URL read, Sentinel parser, connection kwargs, health-check interval. `tests/infra/test_redis_client.py` compares the two composed clients field by field.
- `retries`, connect timeout, socket timeout are keyword arguments with no default — a broker read waits seconds, a cache in front of a database must not.
- No message raised here names a connection URL.
- `ICache`/`IAsyncCache` are the same contract on both call styles. Do not add an `IAsyncCache` that wraps an `ICache` in a thread (blocking call with async signature).
- A cache holds no authority: `get_value`/`set_value` swallow unreachable client; `delete_value` raises. An unreachable client is left alone for `FAILURE_WINDOW_SEC`.
- A backend stores what it was handed and reads it back — no shape imposed, no value coerced.
- The engine mirrors the synchronous store: same URLs and same rotation.
- The repository takes a composed engine, not the holder itself.
- Security context is keyed by `OwnerScope.security_token` and nothing else; renders no value. Neither holder has a default.
- Holders have `initialize` and a loud property, and nothing else. No `reset_for_tests` — a test replaces the consumer module's reference.
- Every parameter carries its own `ttl_sec`; the backend honours it.
- Every root installs `SqlSecurityContextStore`; no other production backend exists. In-memory and discarding stores are test doubles.
- What is collected is declared in the image (`header_collector.yaml`); cookie parsing has one owner: the collector.
- SQL backend stores no value it can read without the key. Token, type, name are in the clear; value is AES-256-SIV ciphertext under `CredentialsKey` — exactly 64 bytes of `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY`, because `AESSIV` would take 32 or 48 and run weaker (`tests/infra/test_credentials_key.py`).
- A save replaces one token's set and touches no other token's rows. Two saves of one token serialize on `pg_advisory_xact_lock`.
- Nothing deletes an expired row but the next save of its token; a load filters on the expiry.
- `IUserTokenStore` is load-only. `SqlUserTokenRepo` (the writer) never reads a token back. Both share `_associated_data` and seal through `CredentialsKey`.
- This package imports no transport, no supervisor, no `multiuser_config`.
