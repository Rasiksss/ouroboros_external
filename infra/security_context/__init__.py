from __future__ import annotations

from infra.security_context.interface import ISecurityContextCollector, ISecurityContextStore


class SecurityContextFactory:
    def __init__(self) -> None:
        self._store: ISecurityContextStore | None = None

    def initialize(self, store: ISecurityContextStore) -> None:
        self._store = store

    @property
    def store(self) -> ISecurityContextStore:
        store = self._store
        if store is None:
            raise RuntimeError(
                "the security context store is not installed; a composition root must call "
                "security_context.initialize() before anything serves"
            )
        return store


class SecurityContextCollectorFactory:
    def __init__(self) -> None:
        self._collector: ISecurityContextCollector | None = None

    def initialize(self, collector: ISecurityContextCollector) -> None:
        self._collector = collector

    @property
    def collector(self) -> ISecurityContextCollector:
        collector = self._collector
        if collector is None:
            raise RuntimeError(
                "the security context collector is not installed; a composition root must call "
                "security_context_collector.initialize() before anything serves"
            )
        return collector


security_context = SecurityContextFactory()
security_context_collector = SecurityContextCollectorFactory()


__all__ = [
    "SecurityContextCollectorFactory",
    "SecurityContextFactory",
    "security_context",
    "security_context_collector",
]
