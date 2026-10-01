"""Registry resolving model strings to provider adapters and remote targets."""

from __future__ import annotations

from ouroboros.llm.models import ModelRef, parse_model_ref
from ouroboros.llm.protocol import ProviderAdapter
from ouroboros.llm.providers import GigachatB2BankProvider, OpenAICompatibleProvider
from ouroboros.llm.targets import RemoteTarget

_DEFAULT_ADAPTERS: list[ProviderAdapter] | None = None


def default_adapters() -> list[ProviderAdapter]:
    global _DEFAULT_ADAPTERS
    if _DEFAULT_ADAPTERS is None:
        _DEFAULT_ADAPTERS = [
            GigachatB2BankProvider(),
            OpenAICompatibleProvider("cloudru"),
            OpenAICompatibleProvider("selfhosted"),
        ]
    return _DEFAULT_ADAPTERS


class ProviderRegistry:
    def __init__(self, adapters: list[ProviderAdapter] | None = None) -> None:
        self._adapters = list(adapters or default_adapters())
        self._closed = False

    def adapter_for(self, ref: ModelRef) -> ProviderAdapter:
        for adapter in self._adapters:
            if adapter.supports(ref):
                self._closed = False
                return adapter
        raise ValueError(f"Unsupported LLM provider for headless runtime: {ref.provider!r}")

    def resolve(self, model: str) -> tuple[ProviderAdapter, RemoteTarget]:
        ref = parse_model_ref(model)
        adapter = self.adapter_for(ref)
        return adapter, adapter.resolve_target(ref)

    def resolve_target(self, model: str) -> RemoteTarget:
        _, target = self.resolve(model)
        return target

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for adapter in self._adapters:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()


_DEFAULT_REGISTRY: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderRegistry()
    return _DEFAULT_REGISTRY
