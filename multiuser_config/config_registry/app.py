from __future__ import annotations

import logging
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config_registry.agent_registry.api.http.endpoint import router as agents_router
from config_registry.chat_registry.api.http.endpoint import router as chats_router
from config_registry.user_settings.api.http.endpoint import router as owner_settings_router
from infra.async_engine import async_engine_factory
from infra.cache import cache_factory, refuse_a_process_local_settings_cache
from infra.migrations.runner import assert_schema_at_head
from infra.utils.http.fastapi import install_trace_middleware
from multiuser_config.bootstrap import start_process_monitoring, stop_process_monitoring
from new_core.skills.catalog import SkillCatalog
from new_core.skills.providers.distribution import DistributionSkillProvider
from new_core.skills.surface import SkillSurface
from ouroboros.app.infra.repositories.deployment_keyspace.repo import SqlDeploymentKeyspaceRepository
from ouroboros.app.multiuser_validation import validate_deployment_keyspace, validate_multiuser_configuration
from ouroboros.gateway.router import collect_routes
from ouroboros.observability.health import ComposedProbes
from ouroboros.observability.logging_setup import configure_logging
from ouroboros.skills.availability import build_skill_availability
from ouroboros.skills.loader import DistributionSkillLoader
from ouroboros.skills.presentation import load_skill_presentations
from ouroboros.storage.blob.guards import validate_production_storage
from ouroboros.storage.engine_setup import refuse_without_runtime_database
from ouroboros.storage.message_field_crypto import validate_chat_encryption
from ouroboros.storage.probe_database import probe_database_factory
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.storage.settings.store import load_settings

log = logging.getLogger(__name__)
TIER = "config-registry"
MOUNT_PATH = "/api/agents"
SETTINGS_MOUNT_PATH = "/api/owner-settings"
CHATS_MOUNT_PATH = "/api/chats"


def build_registry_app() -> FastAPI:
    settings = load_settings()
    configure_logging(level=str(settings.OUROBOROS_LOG_LEVEL or "INFO"))
    start_process_monitoring()
    paths = load_path_settings()
    drive_root = pathlib.Path(paths.data_dir)
    repo_dir = pathlib.Path(paths.repo_dir)
    refuse_without_runtime_database(load_settings())
    validate_production_storage()
    validate_chat_encryption()
    validate_multiuser_configuration()
    refuse_a_process_local_settings_cache(TIER)
    cache_factory.initialize(
        str(settings.OUROBOROS_SETTINGS_CACHE_BACKEND or "none"), int(settings.OUROBOROS_SETTINGS_CACHE_TTL_SEC)
    )
    persistence = runtime_persistence.initialize()
    assert_schema_at_head(persistence.engine)
    validate_deployment_keyspace(SqlDeploymentKeyspaceRepository(persistence.engine))
    probe_database = probe_database_factory.initialize_beside_store(settings)
    runtime_persistence.close()
    async_engine_factory.initialize_beside_store(settings)
    app = FastAPI(
        title="Ouroboros config registry",
        description="The agents an owner has, the settings of each, and the conversations they hold.",
        version="1",
        lifespan=_lifespan,
    )
    app.router.routes.extend(collect_routes(data_dir=drive_root, repo_dir=repo_dir, browser_enabled=False))
    app.include_router(agents_router, prefix=MOUNT_PATH)
    app.include_router(owner_settings_router, prefix=SETTINGS_MOUNT_PATH)
    app.include_router(chats_router, prefix=CHATS_MOUNT_PATH)
    skill_catalog = SkillCatalog()
    skill_catalog.initialize(
        [DistributionSkillProvider(distribution_root=repo_dir / "skills", loader=DistributionSkillLoader(settings))]
    )
    app.state.skill_surface = SkillSurface(
        snapshot=skill_catalog.snapshot(), availability=build_skill_availability(settings)
    )
    app.state.skill_presentations = load_skill_presentations(skill_catalog.snapshot())
    install_trace_middleware(app, source=TIER)
    # This process *is* the tier the probes answer for, and it hosts no loop that could die
    # apart from it — the same answer the transport root gives.
    app.state.health_probes = ComposedProbes(probe_database)

    log.info(
        "Composed the %s tier: mounts=%s,%s,%s drive_root=%s",
        TIER,
        MOUNT_PATH,
        SETTINGS_MOUNT_PATH,
        CHATS_MOUNT_PATH,
        drive_root,
    )
    return app


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await async_engine_factory.dispose()
        await probe_database_factory.dispose()
        stop_process_monitoring()


__all__ = ["CHATS_MOUNT_PATH", "MOUNT_PATH", "SETTINGS_MOUNT_PATH", "TIER", "build_registry_app"]
