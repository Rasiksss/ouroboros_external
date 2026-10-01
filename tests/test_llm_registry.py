from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros.llm import LLMClient
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

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}

        @staticmethod
        def raise_for_status():
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

        def close(self):
            return None

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    del fake_post
    monkeypatch.setattr("ouroboros.llm.providers.gigachat_b2bank.httpx.Client", FakeClient)

    provider = GigachatB2BankProvider()
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
    clock = {"now": 100.0}

    class DelayedSemaphore:
        def __init__(self) -> None:
            self.acquire_timeout: float | None = None
            self.release_calls = 0

        def acquire(self, timeout=None):
            self.acquire_timeout = timeout
            clock["now"] = 106.0
            return True

        def release(self):
            self.release_calls += 1

    semaphore = DelayedSemaphore()
    monkeypatch.setattr("ouroboros.llm.client._llm_semaphore", lambda: semaphore)
    monkeypatch.setattr("ouroboros.llm.client.time.monotonic", lambda: clock["now"])
    adapter = _FakeAdapter()
    client = LLMClient(registry=ProviderRegistry([adapter]))

    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="gigachat-b2bank::fake",
        timeout_sec=30.0,
        deadline=110.0,
    )

    assert semaphore.acquire_timeout == 10.0
    assert adapter.phase_caps == [4.0]
    assert semaphore.release_calls == 1


def test_llm_client_limits_concurrent_chat_calls_to_two(monkeypatch):
    monkeypatch.setattr(
        "ouroboros.storage.settings.store.load_settings",
        lambda: AppSettings(OUROBOROS_LLM_MAX_CONCURRENCY=2),
    )
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
