"""Composition and lifetime of the runtime database and its repositories."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from infra.credentials_key import CredentialsKey
from infra.runtime_database import create_runtime_engine
from infra.sql_transaction import SqlTransactionManager
from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.conversations.repository import SqlConversationRepository
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.create_root_task.use_case import CreateRootTaskUseCase
from ouroboros.storage.db_url import canonicalize_runtime_db_urls, collect_runtime_db_urls
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.execution_memory.repository import SqlExecutionMemoryRepository
from ouroboros.storage.result_delivery.interfaces import IResultDeliveryRepository
from ouroboros.storage.result_delivery.repository import SqlResultDeliveryRepository
from ouroboros.storage.task_plans import SqlTaskPlanRepository
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tasks.repository import SqlTaskRepository

_POOL_HEADROOM = 4


def runtime_pool_size(worker_slots: int) -> int:
    """Reserve connections for concurrent runs, supervisor, memory and transport work."""
    return max(1, int(worker_slots or 1)) * 2 + _POOL_HEADROOM


@dataclass(frozen=True)
class RuntimePersistence:
    engine: Engine
    db_urls: list[str] = field(repr=False)
    tasks_repo: ITaskRepository
    conversations_repo: IConversationRepository
    memory_repo: IExecutionMemoryRepository
    transaction_manager: SqlTransactionManager
    result_repo: IResultDeliveryRepository
    create_root_task: CreateRootTaskUseCase
    plans_repo: SqlTaskPlanRepository

    def close(self) -> None:
        self.engine.dispose()


def compose_runtime_persistence(engine: Engine, *, db_urls: list[str]) -> RuntimePersistence:
    """Compose repositories on one transaction manager and take ownership of the engine."""
    transaction_manager = SqlTransactionManager(engine)
    tasks_repo = SqlTaskRepository(transaction_manager)
    conversations_repo = SqlConversationRepository(transaction_manager)
    memory_repo = SqlExecutionMemoryRepository(transaction_manager)
    return RuntimePersistence(
        engine=engine,
        db_urls=list(db_urls),
        tasks_repo=tasks_repo,
        plans_repo=SqlTaskPlanRepository(transaction_manager, _plan_credentials_key),
        conversations_repo=conversations_repo,
        memory_repo=memory_repo,
        transaction_manager=transaction_manager,
        result_repo=SqlResultDeliveryRepository(transaction_manager),
        create_root_task=CreateRootTaskUseCase(
            tasks_repo=tasks_repo,
            conversations_repo=conversations_repo,
            memory_repo=memory_repo,
            scratchpad_repo=SqlScratchpad(engine),
            transaction_manager=transaction_manager,
            episode_limit=_episode_limit,
        ),
    )


def create_runtime_persistence(*, configured_urls: list[str], worker_slots: int | None = None) -> RuntimePersistence:
    db_urls = canonicalize_runtime_db_urls(list(configured_urls))
    slots = int(worker_slots) if worker_slots is not None else _configured_worker_slots()
    pool_size = runtime_pool_size(slots)
    engine = create_runtime_engine(db_urls, pool_size=pool_size, max_overflow=pool_size)
    try:
        return compose_runtime_persistence(engine, db_urls=db_urls)
    except BaseException:
        engine.dispose()
        raise


def _configured_worker_slots() -> int:
    # Settings bootstrap imports this module before configuration is installed.
    from ouroboros.storage.settings.store import load_settings

    try:
        return max(1, int(load_settings().OUROBOROS_WORKER_SLOTS or 1))
    except Exception:
        return 1


def _plan_credentials_key() -> CredentialsKey:
    # Settings bootstrap imports composition before the configuration is installed.
    from ouroboros.storage.settings.store import load_settings

    return CredentialsKey(load_settings().OUROBOROS_CREDENTIALS_ENCRYPTION_KEY)


def _episode_limit() -> int:
    # Settings bootstrap imports this module; resolve configuration after composition.
    from ouroboros.storage.settings.store import load_settings

    return load_settings().OUROBOROS_EPISODE_RETRIEVAL_CANDIDATES


def _configured_db_urls() -> list[str]:
    # Settings bootstrap imports this module; resolve configuration after composition.
    from ouroboros.storage.settings.store import load_settings

    return collect_runtime_db_urls(load_settings())


class RuntimePersistenceProvider:
    """Hold the process composition until its database users have stopped."""

    def __init__(self) -> None:
        self._persistence: RuntimePersistence | None = None

    def initialize(self) -> RuntimePersistence:
        if self._persistence is None:
            self._persistence = create_runtime_persistence(configured_urls=_configured_db_urls())
        return self._persistence

    @property
    def current(self) -> RuntimePersistence:
        if self._persistence is None:
            raise RuntimeError(
                "runtime persistence is not composed: the composition root must call runtime_persistence.initialize()"
            )
        return self._persistence

    def close(self) -> None:
        persistence, self._persistence = self._persistence, None
        if persistence is not None:
            persistence.close()


runtime_persistence = RuntimePersistenceProvider()
