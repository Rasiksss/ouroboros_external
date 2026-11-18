"""
A2A — Server module.

Runs a separate Starlette/uvicorn server on A2A_PORT (default 18800).
Serves the dynamic Agent Card and handles A2A JSON-RPC requests.
Disabled by default (A2A_ENABLED=False). Enable via environment variables.
Requires restart when toggled.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import socket
from typing import TYPE_CHECKING, Any

from background_memory.interfaces import IMemoryEventPublisher
from ouroboros.storage.settings.schema import SettingsLike
from transport.a2a.interfaces import IAgentSkillCatalog, ISupervisorReadiness
from transport.interfaces import IMessageGateway

if TYPE_CHECKING:
    from a2a.server.request_handlers.request_handler import RequestHandler
    from a2a.types.a2a_pb2 import AgentCard

_uvicorn: Any = None
_Starlette: Any = None
_create_agent_card_routes: Any = None
_DefaultRequestHandler: Any = None
_A2AStarletteApplication: Any = None
_AgentCardCls: Any = None
_AgentSkillCls: Any = None
_DefaultServerCallContextBuilder: Any = None

try:
    import uvicorn as _uvicorn_mod
    from a2a.server.request_handlers import DefaultRequestHandler as _DefaultRequestHandler_mod
    from starlette.applications import Starlette as _Starlette_mod

    try:
        from a2a.server.routes.agent_card_routes import (
            create_agent_card_routes as _create_agent_card_routes_mod,
        )
        from a2a.server.routes.common import (
            DefaultServerCallContextBuilder as _DefaultServerCallContextBuilder_mod,
        )
        from a2a.types.a2a_pb2 import AgentCard as _AgentCardCls_mod, AgentSkill as _AgentSkillCls_mod

        _DefaultServerCallContextBuilder = _DefaultServerCallContextBuilder_mod
        _create_agent_card_routes = _create_agent_card_routes_mod
        _AgentCardCls = _AgentCardCls_mod
        _AgentSkillCls = _AgentSkillCls_mod
    except ImportError:
        from a2a.server.apps.jsonrpc.starlette_app import (
            A2AStarletteApplication as _A2AStarletteApplication_mod,
        )
        from a2a.types import AgentCard as _AgentCardCls_mod, AgentSkill as _AgentSkillCls_mod

        _A2AStarletteApplication = _A2AStarletteApplication_mod
        _AgentCardCls = _AgentCardCls_mod
        _AgentSkillCls = _AgentSkillCls_mod

    _uvicorn = _uvicorn_mod
    _Starlette = _Starlette_mod
    _DefaultRequestHandler = _DefaultRequestHandler_mod
    _A2A_AVAILABLE = True
except ImportError:
    _A2A_AVAILABLE = False

from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import OuroborosA2AExecutor, resubmit_unprocessed
from transport.a2a.proto import build_agent_card_proto, new_agent_skill
from transport.a2a.task_store import RuntimeDbA2ATaskStore

log = logging.getLogger("a2a-server")

_server: Any = None
_cleanup_task: asyncio.Task | None = None
_router: A2AResponseRouter | None = None


def _resolve_host(configured_host: str) -> str:
    if configured_host in ("0.0.0.0", "::"):
        try:
            return socket.getfqdn() or socket.gethostname() or "localhost"
        except Exception:
            return "localhost"
    return configured_host


def _fallback_skills() -> list[Any]:
    """What the card advertises while the catalog has nothing to say yet.

    Not cached and not an error: the usual reason for an empty catalog is that
    startup is still under way, so the next card request must ask again.
    """
    return [
        new_agent_skill(
            "general",
            "General Assistant",
            "Code editing, analysis, git operations, file management",
            ["general"],
        )
    ]


def _card_skills(catalog: IAgentSkillCatalog) -> list[Any]:
    """The catalog's skills as A2A proto skills, or the fallback."""
    try:
        specs = catalog.skills()
    except Exception:
        log.warning("The agent skill catalog failed; publishing the fallback card", exc_info=True)
        return _fallback_skills()
    skills = [new_agent_skill(spec.id, spec.name, spec.description[:200], list(spec.tags)) for spec in specs]
    return skills or _fallback_skills()


def _build_agent_card(
    settings: SettingsLike,
    host: str,
    port: int,
    catalog: IAgentSkillCatalog,
) -> Any:
    """Build the card from deployment settings and the live skill catalog."""
    name = settings.get("A2A_AGENT_NAME", "").strip() or "ГигаАгент"
    description = settings.get("A2A_AGENT_DESCRIPTION", "").strip() or "Personal AI agent"

    public_url = str(settings.get("A2A_PUBLIC_URL", "") or "").strip()
    if public_url:
        service_url = public_url.rstrip("/") + "/"
    else:
        resolved_host = _resolve_host(host)
        service_url = f"http://{resolved_host}:{port}/"

    return build_agent_card_proto(
        name=name,
        description=description,
        version="",
        service_url=service_url,
        skills=_card_skills(catalog),
    )


def _refresh_skills(card: Any, catalog: IAgentSkillCatalog) -> Any:
    """The SDK's card modifier: republish the live skills on every card request."""
    live_skills = _card_skills(catalog)
    if live_skills and live_skills[0].id != "general":
        try:
            card.skills = live_skills
        except Exception:
            del card.skills[:]
            card.skills.extend(live_skills)
    return card


def build_call_context_builder() -> Any:
    """The per-request carrier the executor reads its trace id off.

    The SDK default already puts `dict(request.headers)` on `state["headers"]`, but that is an
    implementation detail of one version of one entry point, and an ingress that silently stops
    correlating when a dependency changes its default is the defect this exists to remove. So the
    guarantee is asserted here: delegate, then make sure the key is there.
    """

    class OuroborosServerCallContextBuilder(_DefaultServerCallContextBuilder):
        def build(self, request: Any) -> Any:
            context = super().build(request)
            context.state.setdefault("headers", dict(request.headers))
            return context

    return OuroborosServerCallContextBuilder()


def _build_starlette_app(
    agent_card: AgentCard,
    handler: RequestHandler,
    catalog: IAgentSkillCatalog,
    *,
    max_chars: int,
) -> Any:
    async def card_modifier(card: Any) -> Any:
        return _refresh_skills(card, catalog)

    if _A2AStarletteApplication is not None:
        # Unreachable under the pinned `a2a-sdk[http-server]==1.0.3`, which ships the routes
        # module above; the builder is passed here for symmetry where the constructor takes one.
        legacy_kwargs: dict[str, Any] = {}
        if _DefaultServerCallContextBuilder is not None and (
            "context_builder" in inspect.signature(_A2AStarletteApplication).parameters
        ):
            legacy_kwargs["context_builder"] = build_call_context_builder()
        return _A2AStarletteApplication(
            agent_card=agent_card,
            http_handler=handler,
            card_modifier=card_modifier,
            **legacy_kwargs,
        ).build(
            agent_card_url="/.well-known/agent-card.json",
            rpc_url="/",
        )

    routes = []
    routes.extend(
        _create_agent_card_routes(
            agent_card=agent_card,
            card_modifier=card_modifier,
        )
    )
    # This route is composed only when the optional A2A HTTP SDK is available.
    from starlette.routing import Route

    from transport.a2a.inbound import InboundJsonRpcDispatcher

    dispatcher = InboundJsonRpcDispatcher(handler, max_chars=max_chars, context_builder=build_call_context_builder())
    routes.append(Route(path="/", endpoint=dispatcher.handle_requests, methods=["POST"]))
    return _Starlette(routes=routes)


async def _task_cleanup_loop(store: RuntimeDbA2ATaskStore, interval_sec: int = 3600) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            removed = await store.cleanup_expired()
            if removed:
                log.info("A2A task cleanup: removed %d expired tasks", removed)
        except Exception:
            log.warning("A2A task cleanup error", exc_info=True)


async def start_a2a_server(
    settings: SettingsLike,
    *,
    gateway: IMessageGateway,
    drive_root: pathlib.Path,
    skill_catalog: IAgentSkillCatalog,
    readiness: ISupervisorReadiness,
    session_id: str,
    memory_events: IMemoryEventPublisher,
) -> None:
    global _server, _cleanup_task, _router

    if not _A2A_AVAILABLE:
        log.warning(
            "a2a-sdk is not installed — A2A server cannot start. "
            "Install with: pip install 'a2a-sdk[http-server]==1.0.3'"
        )
        return

    host = str(settings.get("A2A_HOST", "127.0.0.1")).strip()
    port = int(settings.get("A2A_PORT", 18800))
    max_concurrent = int(settings.get("A2A_MAX_CONCURRENT", 3))
    ttl_hours = int(settings.get("A2A_TASK_TTL_HOURS", 24))

    log.info("Starting A2A server on %s:%d", host, port)

    task_store = RuntimeDbA2ATaskStore(ttl_hours=ttl_hours)
    # Before anything is served: a task still `working` in the store belongs to a
    # process that died holding it, and nothing else ever moves it out of there.
    await task_store.fail_orphaned_tasks()
    router = A2AResponseRouter(gateway.outbox)
    router.start()
    _router = router
    executor = OuroborosA2AExecutor(
        max_concurrent=max_concurrent,
        task_store=task_store,
        gateway=gateway,
        router=router,
        readiness=readiness,
        session_id=session_id,
        memory_events=memory_events,
        drive_root=drive_root,
    )
    # Anything persisted but never handed over — a submit that failed, a process
    # that died between the two — is submitted again here.
    await asyncio.to_thread(resubmit_unprocessed, gateway, drive_root)
    agent_card = _build_agent_card(settings, host, port, skill_catalog)

    handler_kwargs: dict[str, Any] = {
        "agent_executor": executor,
        "task_store": task_store,
    }
    if "agent_card" in inspect.signature(_DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = agent_card
    handler = _DefaultRequestHandler(**handler_kwargs)
    starlette_app = _build_starlette_app(
        agent_card, handler, skill_catalog, max_chars=int(settings["OUROBOROS_MAX_INBOUND_MESSAGE_CHARS"])
    )

    config = _uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    _server = _uvicorn.Server(config)
    local_cleanup_task = asyncio.create_task(_task_cleanup_loop(task_store), name="a2a-task-cleanup")
    _cleanup_task = local_cleanup_task
    try:
        await _server.serve()
    except Exception:
        log.error("A2A server on %s:%d exited with error", host, port, exc_info=True)
    finally:
        # Off the loop: `stop` joins the reader thread, which may be mid-delivery.
        # Shielded so a cancelled serve task still releases the subscription.
        await asyncio.shield(asyncio.create_task(asyncio.to_thread(router.stop)))
        if not local_cleanup_task.done():
            local_cleanup_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(local_cleanup_task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass


def stop_a2a_server() -> None:
    global _server, _cleanup_task, _router
    if _server:
        _server.should_exit = True
        _server = None
    if _router is not None:
        # Signal only: this runs on the event loop, and the serving task joins
        # the reader thread off it as it unwinds.
        _router.request_stop()
        _router = None
    _cleanup_task = None
    log.info("A2A server shutdown requested")
