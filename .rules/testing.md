---
paths:
  - "tests/**"
  - "tests_headless/**"
  - "skills/**/tests/**"
  - "tests/helpers/**"
---

# Tests — read before editing

| Concern | Owner |
|---|---|
| Shared test doubles (FakeGateway, RecordingOutbox, the Kafka double and its recording factory, etc.) | `tests/helpers/fake_gateway.py`, `tests/helpers/outbound.py`, `tests/helpers/agent_events.py`, `tests/helpers/kafka_double.py` (with `kafka_double_clients.py`), `tests/helpers/kafka_helpers.py`, `tests/helpers/kafka_recording.py`, and the behaviour suite the double and a real broker both run, `tests/helpers/kafka_client_behaviour.py` with `kafka_group_behaviour.py` |
| Factory functions for mocks (make_mock_llm, make_loop_ctx, etc.) | `tests/helpers/factories.py` |
| HTTP doubles (FakeHttpResponse, RecordingHttpClient) | `tests/helpers/http_double.py` |
| Comparison DSL (assert_state_subset, task_record_eq, etc.) | `tests/helpers/comparisons.py` |
| `parametrize_from_dict` | `tests/helpers/parametrize.py` |
| Loop test harness (drive_loop) | `tests/helpers/loop_harness.py` |
| Shared constants (TEST_SCOPE, make_tool_ctx, etc.) | `tests/helpers/shared.py` |
| Conformance test base classes | `tests/helpers/transport_conformance.py`, `agent_event_conformance.py`, `budget_conformance.py`, `memory_event_conformance.py` |

**Rules:**

- **Mirror structure** — `tests/<source_package>/test_<module>.py`, not flat `tests/test_<name>.py`.
A new test goes in the mirror directory of the source package it tests; cross-cutting tests go in
`tests/e2e/`, AST-guard tests in `tests/meta/`, server lifecycle in `tests/server/`.

- **One file = one source module or one capability.** If a test file exceeds 500 lines, split it by
concern. The split is proven by the same narrow tests passing after the move.

- **`parametrize_from_dict` is the canonical parametrization pattern.** Dict keys are
human-readable test IDs, values are parameter dicts. Prefer it over raw `pytest.mark.parametrize`
for 3+ variants of one logic. Test cases can be module-level dict constants (like `_NON_TOKEN_NAMES`).
Do not combine independent contracts only to reduce the number of test functions.

- **Factory functions, not conftest fixtures, for test data and mocks.** Factories work at module
load time inside `parametrize_from_dict` dicts; fixtures do not. Use `tests/helpers/factories.py`
(`make_mock_llm`, `make_loop_ctx`, `permissive_safety`, `make_openai_compat_fake`, `make_async_mock`).
Conftest fixtures are for infrastructure only: config bootstrap, runtime store, event loop,
storage/cache resets — anything a test would otherwise repeat in every fixture.

- **One contract per function.** Use as many related assertions as the behavior needs. When repeated
asserts compare fields of one object, prefer an expected shape and a comparison function from `tests/helpers/comparisons.py`
(`assert_state_subset`, `task_record_eq`, `conversation_message_eq`). Comparison functions ignore
dynamic fields (IDs, timestamps, worker assignments) and report the first mismatch with a readable
message.

- **DI over monkeypatch.** If production code accepts an interface, inject a fake via constructor
argument or factory function, not `monkeypatch.setattr`. `monkeypatch` is acceptable only for
stdlib (`time`, `subprocess`, `os`, `pathlib`), third-party library internals (`httpx.Client`,
`boto3`, `openai`), and module-level constants (`_MAX_*`, `_TTL`).

- **`monkeypatch.setenv` is safe in test bodies** — the autouse `_bootstrap_config_for_tests`
fixture wraps `monkeypatch.setenv`/`delenv` to auto-refresh the config singleton, so every env
change is immediately visible to `load_settings()`. Call `reset_memory_settings_for_tests()`
afterwards when the test reads cached settings that mirror env vars.

- **autouse = infrastructure only.** Every autouse fixture in `conftest.py` sets up or tears down
process-wide state (config, event loop, runtime store, caches, storage provider, log handlers).
A test of a pure function does not add a new autouse fixture; it opts in via `usefixtures`.

- **Shared test doubles in `tests/helpers/`.** No `from tests.test_other import helper`. If two test
files need the same helper, extract it to `tests/helpers/` first. The `test_to_test` import is a
debt ratchet — do not add new ones.

- **Conformance tests** — base class in `tests/helpers/` (e.g. `OutboxConformance`), implementation
in `tests/<package>/test_<impl>_conformance.py`. A new transport implementation adds one
conformance test, not a copy of the existing suite.

- **`tmp_path`/`monkeypatch`** — never real `~/Ouroboros/data`; reuse existing test helpers from
`tests/helpers/shared.py` (`TEST_SCOPE`, `make_tool_ctx`, `make_tool_env`).

- **HTTP doubles** — use `tests/helpers/http_double.py` (`FakeHttpResponse`,
`RecordingHttpClient`). Do not define a per-file `FakeClient`/`FakeResponse`; the shared doubles
cover every observed usage pattern (`post`, `request`, `stream`, `close`, queued exceptions,
`raise_for_status`).
