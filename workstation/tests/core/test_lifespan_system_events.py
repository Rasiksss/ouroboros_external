"""Тесты публикации system-events из lifespan приложения."""

from __future__ import annotations

from fastapi import FastAPI

import app.core.lifespan as lifespan_module
from app.services.periodic_tasks import StartupMaintenanceResult


class FakeScheduler:
    """Минимальный scheduler для проверки порядка lifespan-вызовов."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        """Отметить запуск scheduler'а."""
        self.started = True

    def shutdown(self) -> None:
        """Отметить остановку scheduler'а."""
        self.stopped = True


class FakeSystemEventPublisher:
    """Fake startup publisher для lifespan-теста."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def publish_workstation_start(self) -> None:
        """Отметить публикацию startup-события."""
        self.started = True

    async def close(self) -> None:
        """Отметить закрытие publisher'а."""
        self.closed = True


async def test_lifespan_publishes_workstation_start_before_scheduler(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    scheduler = FakeScheduler()
    publisher = FakeSystemEventPublisher()

    def _init_engine() -> None:
        calls.append("init")

    async def _run_startup_maintenance() -> StartupMaintenanceResult:
        calls.append("maintenance")
        return StartupMaintenanceResult(
            failed_missing_rootfs=0,
            removed_orphans=0,
            cleaned_expired=0,
        )

    class _SchedulerFactory(FakeScheduler):
        def __new__(cls):  # type: ignore[no-untyped-def]
            calls.append("scheduler-create")
            return scheduler

    async def _dispose_engine() -> None:
        calls.append("dispose")

    async def _publish_start() -> None:
        calls.append("publish-start")
        publisher.started = True

    def _scheduler_start() -> None:
        calls.append("scheduler-start")
        scheduler.started = True

    def _scheduler_shutdown() -> None:
        calls.append("scheduler-shutdown")
        scheduler.stopped = True

    async def _publisher_close() -> None:
        calls.append("publisher-close")
        publisher.closed = True

    monkeypatch.setattr(lifespan_module, "init_engine", _init_engine)
    monkeypatch.setattr(lifespan_module, "run_startup_maintenance", _run_startup_maintenance)
    monkeypatch.setattr(lifespan_module, "PeriodicTasks", _SchedulerFactory)
    monkeypatch.setattr(lifespan_module, "dispose_engine", _dispose_engine)
    monkeypatch.setattr(publisher, "publish_workstation_start", _publish_start)
    monkeypatch.setattr(publisher, "close", _publisher_close)
    monkeypatch.setattr(scheduler, "start", _scheduler_start)
    monkeypatch.setattr(scheduler, "shutdown", _scheduler_shutdown)
    monkeypatch.setattr(lifespan_module, "system_event_publisher", publisher)

    async with lifespan_module.lifespan(FastAPI()) as _:
        assert publisher.started is True
        assert scheduler.started is True

    assert scheduler.stopped is True
    assert publisher.closed is True
    assert calls == [
        "init",
        "scheduler-create",
        "maintenance",
        "publish-start",
        "scheduler-start",
        "scheduler-shutdown",
        "publisher-close",
        "dispose",
    ]
