from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.llm import LLMCallCancelled, LLMClient
from ouroboros.llm.providers import GigachatB2BankProvider, OpenAICompatibleProvider
from ouroboros.llm.providers.gigachat_b2bank import gigachat_b2bank_credentials_ready
from ouroboros.llm.registry import ProviderRegistry
from ouroboros.storage.settings.schema import AppSettings


def test_registry_resolves_cloudru(monkeypatch):
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru-key")
    registry = ProviderRegistry()
    adapter, target = registry.resolve("cloudru::zai-org/GLM-4.7")
    assert isinstance(adapter, OpenAICompatibleProvider)
    assert adapter.provider_id == "cloudru"
    assert target["provider"] == "cloudru"
    assert target["resolved_model"] == "zai-org/GLM-4.7"


def test_registry_resolves_gigachat(monkeypatch):
    monkeypatch.setenv("GIGACHAT_B2BANK_BASE_URL", "https://giga.example")
    registry = ProviderRegistry()
    adapter, target = registry.resolve("gigachat-b2bank::GigaChat-Pro")
    assert isinstance(adapter, GigachatB2BankProvider)
    assert target["provider"] == "gigachat-b2bank"
    assert target["resolved_model"] == "GigaChat-Pro"
    assert target["chat_api_version"] == "v1"


def test_registry_resolves_gigachat_v2(monkeypatch):
    monkeypatch.setenv("GIGACHAT_B2BANK_BASE_URL", "https://giga.example/v1")
    monkeypatch.setenv("GIGACHAT_B2BANK_CHAT_API_VERSION", "v2")

    _, target = ProviderRegistry().resolve("gigachat-b2bank::GigaChat-Pro")

    assert target["chat_api_version"] == "v2"


def test_gigachat_api_version_rejects_unknown_values():
    with pytest.raises(ValueError, match="GIGACHAT_B2BANK_CHAT_API_VERSION"):
        AppSettings(GIGACHAT_B2BANK_CHAT_API_VERSION="v3")


def test_gigachat_credentials_ready_without_mtls_when_verify_ssl_false():
    settings = AppSettings(
        GIGACHAT_B2BANK_BASE_URL="https://giga.example",
        GIGACHAT_B2BANK_VERIFY_SSL=False,
    )
    assert gigachat_b2bank_credentials_ready(settings) is True


def test_gigachat_credentials_allow_optional_mtls_when_verify_ssl_true():
    settings = AppSettings(
        GIGACHAT_B2BANK_BASE_URL="https://giga.example",
        GIGACHAT_B2BANK_VERIFY_SSL=True,
    )
    assert gigachat_b2bank_credentials_ready(settings) is True


def test_gigachat_provider_sends_client_cert_when_verify_ssl_false(monkeypatch):
    monkeypatch.setenv("GIGACHAT_B2BANK_BASE_URL", "https://giga.example/v1")
    monkeypatch.setenv("GIGACHAT_B2BANK_VERIFY_SSL", "false")
    monkeypatch.setenv("GIGACHAT_B2BANK_CERT_PATH", "/tmp/client.pem")
    monkeypatch.setenv("GIGACHAT_B2BANK_KEY_PATH", "/tmp/client.key")

    captured: dict = {"client_kwargs": {}}

    from tests.helpers.http_double import FakeHttpResponse, RecordingHttpClient

    response = FakeHttpResponse(payload={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})

    class _CapturingClient(RecordingHttpClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["client_kwargs"] = kwargs

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return response

    provider = GigachatB2BankProvider(client_factory=_CapturingClient)
    _, target = ProviderRegistry().resolve("gigachat-b2bank::GigaChat-Pro")
    provider.chat(
        target,
        [{"role": "user", "content": "hi"}],
        tools=None,
        reasoning_effort="low",
        max_tokens=128,
        tool_choice="auto",
        temperature=None,
        no_proxy=False,
    )

    assert captured["client_kwargs"]["cert"] == ("/tmp/client.pem", "/tmp/client.key")
    assert captured["client_kwargs"]["verify"] is False


def test_registry_selfhosted_requires_env(monkeypatch):
    monkeypatch.delenv("SELFHOSTED_LLM_BASE_URL", raising=False)
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        registry.resolve("my-model")


class _FakeAdapter:
    provider_id = "gigachat-b2bank"

    def __init__(self) -> None:
        self.read_timeouts: list[float | None] = []
        self.phase_caps: list[float | None] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def supports(self, ref):
        return ref.provider == self.provider_id

    def resolve_target(self, ref):
        return {"provider": self.provider_id, "resolved_model": ref.model_id}

    def chat(self, target, messages, **kwargs):
        del target, messages
        self.read_timeouts.append(kwargs.get("read_timeout_sec"))
        self.phase_caps.append(kwargs.get("phase_cap_sec"))
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            return {"content": "ok"}, {}
        finally:
            with self.lock:
                self.active -= 1


class _ClosableAdapter(_FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_provider_registry_closes_adapters_idempotently():
    adapter = _ClosableAdapter()
    registry = ProviderRegistry([adapter])

    registry.close()
    registry.close()

    assert adapter.close_calls == 1

    registry.resolve("gigachat-b2bank::fake")
    registry.close()

    assert adapter.close_calls == 2


def test_openai_compatible_provider_closes_cached_clients_idempotently():
    class FakeClient:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    provider = OpenAICompatibleProvider("selfhosted")
    client = FakeClient()
    provider._remote_clients[("key",)] = client

    provider.close()
    provider.close()

    assert client.close_calls == 1


def test_llm_client_passes_timeout_as_phase_cap():
    adapter = _FakeAdapter()
    client = LLMClient(registry=ProviderRegistry([adapter]))

    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="gigachat-b2bank::fake",
        timeout_sec=7.5,
    )

    assert adapter.read_timeouts == [None]
    assert adapter.phase_caps == [7.5]


def test_llm_client_recomputes_timeout_after_concurrency_wait(monkeypatch):
    """The wait for a budget slot comes off the call's own deadline, not out of nowhere."""
    from new_core.coordination.interfaces import IConcurrencyBudget, Lease

    clock = {"now": 100.0}

    class DelayedBudget(IConcurrencyBudget):
        def __init__(self) -> None:
            self.acquire_timeout: float | None = None
            self.release_calls = 0

        def acquire(self, budget, *, holder, ttl_sec, timeout_sec=None):
            self.acquire_timeout = timeout_sec
            clock["now"] = 106.0
            return Lease(budget=budget, holder=holder, token="t", expires_at=0.0)

        async def acquire_async(self, budget, *, holder, ttl_sec, timeout_sec=None):
            return self.acquire(budget, holder=holder, ttl_sec=ttl_sec, timeout_sec=timeout_sec)

        def release(self, lease):
            self.release_calls += 1

        def limit(self, budget):
            return 1

        def in_use(self, budget):
            return 0

    semaphore = DelayedBudget()
    from new_core.coordination.registry import concurrency_budgets

    concurrency_budgets.initialize(semaphore)
    monkeypatch.setattr("ouroboros.llm.client.time.monotonic", lambda: clock["now"])
    adapter = _FakeAdapter()
    client = LLMClient(registry=ProviderRegistry([adapter]))

    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="gigachat-b2bank::fake",
        timeout_sec=30.0,
        deadline=110.0,
    )

    assert semaphore.acquire_timeout == 1.0
    assert adapter.phase_caps == [4.0]
    assert semaphore.release_calls == 1
    concurrency_budgets.reset_for_tests()


def test_llm_client_limits_concurrent_chat_calls_to_two(monkeypatch):
    from new_core.coordination.inprocess import InProcessBudget
    from new_core.coordination.registry import MODEL_CALLS, TOOL_BODIES, concurrency_budgets

    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 2, TOOL_BODIES: 8}))
    adapter = _FakeAdapter()
    client = LLMClient(registry=ProviderRegistry([adapter]))

    def call_once():
        return client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gigachat-b2bank::fake",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(call_once) for _ in range(3)]
        for future in futures:
            assert future.result()[0]["content"] == "ok"

    assert adapter.max_active == 2
    concurrency_budgets.reset_for_tests()


def test_stop_during_model_slot_acquisition_releases_the_grant_without_dispatch():
    """A stop that races with a successful budget grant still prevents provider dispatch."""
    from new_core.coordination.inprocess import InProcessBudget
    from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets
    from ouroboros.llm import LLMCallCancelled

    stopped = threading.Event()

    class StoppingBudget(InProcessBudget):
        def acquire(self, *args, **kwargs):
            lease = super().acquire(*args, **kwargs)
            stopped.set()
            return lease

    budgets = StoppingBudget({MODEL_CALLS: 1})
    concurrency_budgets.initialize(budgets)
    adapter = _FakeAdapter()
    client = LLMClient(registry=ProviderRegistry([adapter]))
    try:
        with pytest.raises(LLMCallCancelled):
            client.chat(messages=[], model="gigachat-b2bank::fake", stop_requested=stopped.is_set)
        assert adapter.phase_caps == []
        assert budgets.in_use(MODEL_CALLS) == 0
    finally:
        concurrency_budgets.reset_for_tests()


@pytest.mark.parametrize("loss", ["claim", "stop"])
def test_llm_slot_wait_uses_configured_interval_and_observes_authority_without_deadline(tmp_path, monkeypatch, loss):
    from new_core.coordination.inprocess import InProcessBudget
    from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import execution_claim
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="waiting", detached=True, scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="test")
    budgets = InProcessBudget({MODEL_CALLS: 1})
    held = budgets.acquire(MODEL_CALLS, holder="occupied", ttl_sec=60)
    concurrency_budgets.initialize(budgets)
    monkeypatch.setattr(
        "ouroboros.storage.settings.store.load_settings", lambda: AppSettings(OUROBOROS_LLM_CLAIM_POLL_INTERVAL_SEC=0.5)
    )
    entered, stopped = threading.Event(), threading.Event()
    acquire = budgets.acquire
    waits = []

    def waiting(*args, **kwargs):
        waits.append(kwargs["timeout_sec"])
        entered.set()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(budgets, "acquire", waiting)
    adapter = _FakeAdapter()

    def call():
        token = execution_claim.set((claimed.task_id, claimed.metadata["execution_claim"], stopped.is_set))
        try:
            LLMClient(registry=ProviderRegistry([adapter])).chat(
                messages=[{"role": "user", "content": "hi"}], model="gigachat-b2bank::fake"
            )
        finally:
            execution_claim.reset(token)

    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(call)
            assert entered.wait(2)
            if loss == "stop":
                stopped.set()
            else:
                claimed.metadata["execution_claim"] = "replacement"
                store.tasks_repo.upsert_task(claimed)
            with pytest.raises(LLMCallCancelled, match="authority lost"):
                future.result(timeout=2)
        assert waits == [0.5]
        assert adapter.phase_caps == []
    finally:
        budgets.release(held)
        concurrency_budgets.reset_for_tests()
