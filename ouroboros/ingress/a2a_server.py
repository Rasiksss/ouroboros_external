"""
A2A — Server module.

Runs a separate Starlette/uvicorn server on A2A_PORT (default 18800).
Serves the dynamic Agent Card and handles A2A JSON-RPC requests.
Disabled by default (A2A_ENABLED=False). Enable via environment variables.
Requires restart when toggled.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

from new_core.tools.catalog import tool_catalog
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.settings.schema import SettingsLike

_uvicorn: Any = None
_Starlette: Any = None
_create_agent_card_routes: Any = None
_create_jsonrpc_routes: Any = None
_DefaultRequestHandler: Any = None
_A2AStarletteApplication: Any = None
_AgentCardCls: Any = None
_AgentSkillCls: Any = None

try:
    import uvicorn as _uvicorn_mod
    from a2a.server.request_handlers import DefaultRequestHandler as _DefaultRequestHandler_mod
    from starlette.applications import Starlette as _Starlette_mod

    try:
        from a2a.server.routes.agent_card_routes import (
            create_agent_card_routes as _create_agent_card_routes_mod,
        )
        from a2a.server.routes.jsonrpc_routes import (
            create_jsonrpc_routes as _create_jsonrpc_routes_mod,
        )
        from a2a.types.a2a_pb2 import AgentCard as _AgentCardCls_mod, AgentSkill as _AgentSkillCls_mod

        _create_agent_card_routes = _create_agent_card_routes_mod
        _create_jsonrpc_routes = _create_jsonrpc_routes_mod
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

from ouroboros.ingress.a2a_executor import OuroborosA2AExecutor
from ouroboros.ingress.a2a_proto import build_agent_card_proto, new_agent_skill
from ouroboros.ingress.a2a_task_store import SQLiteA2ATaskStore

log = logging.getLogger("a2a-server")

_server: Any = None
_cleanup_task: asyncio.Task | None = None


def _resolve_host(configured_host: str) -> str:
    if configured_host in ("0.0.0.0", "::"):
        try:
            return socket.getfqdn() or socket.gethostname() or "localhost"
        except Exception:
            return "localhost"
    return configured_host


def _load_identity_text() -> str:
    """The identity document, or ``""`` when the capability cannot produce it.

    Read through the capability, not off the filesystem: under the ``s3``
    backend there is no local file, and the card used to come back unnamed.

    Blocking: this is a runtime-DB round trip, and on a deployment whose
    identity row is absent ``load_identity`` seeds it, so it can open a write
    transaction too. Callers on the event loop must go through
    ``asyncio.to_thread``.

    An unnamed card and a card built from an identity with no heading look
    identical from outside, so the degradation is logged with its traceback —
    without it a permanently unnamed agent is invisible to the operator.
    """
    try:
        return get_memory().load_identity()
    except Exception:
        log.warning("Could not read the identity document; the agent card will be unnamed", exc_info=True)
        return ""


def _parse_identity(content: str) -> tuple[str, str]:
    """Derive the agent card's name and description from the identity document."""
    lines = content.strip().splitlines()
    name = ""
    heading_found = False
    desc_lines: list[str] = []
    for line in lines:
        if not heading_found and line.startswith("# "):
            heading_found = True
            raw = line.lstrip("# ").strip()
            candidate = re.sub(r"^I\s+Am\s+", "", raw, flags=re.IGNORECASE).strip()
            if candidate.lower() not in ("who i am", "about me", "identity"):
                name = candidate
            continue
        if heading_found and not desc_lines and not line.strip():
            continue
        if heading_found and line.startswith("---"):
            break
        if heading_found and line.startswith("## "):
            break
        if heading_found and line.strip():
            if not name:
                m = re.match(r"^I(?:'m|\s+am)\s+(\w+)", line.strip(), re.IGNORECASE)
                if m:
                    name = m.group(1)
            desc_lines.append(line.strip())
            if len(desc_lines) >= 3:
                break
    return name, " ".join(desc_lines)


#: The published skill list, cached after the first build that saw a wired catalog.
#: ``_refresh_skills`` runs as the SDK's ``card_modifier``, i.e. on every agent-card
#: request. The published set is the core tools, which do not change while the process
#: runs, so the read is worth doing exactly once.
_SKILLS_CACHE: list[Any] | None = None


def _build_skills_from_registry() -> list[Any]:
    """Publish the agent's core tools as A2A skills, read off the tool catalog."""
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None:
        return list(_SKILLS_CACHE)
    try:
        from ouroboros.policy.capabilities import CORE_TOOL_NAMES
        from ouroboros.tools.catalog import spec_for_tool

        # The process-wide catalog, not one built here: the card used to assemble a third
        # catalog from the agent's roots, which re-walked the skills tree and could
        # disagree with the one tasks actually run against. An empty list means the
        # lifespan has not wired it yet, so fall back and stay retryable.
        if not tool_catalog.providers:
            raise RuntimeError("tool catalog not initialized")
        skills = []
        for desc in tool_catalog.descriptors():
            tool_name = desc.name
            if not tool_name or tool_name not in CORE_TOOL_NAMES:
                continue
            if spec_for_tool(tool_name).surface == "excluded":  # disabled via OUROBOROS_DISABLED_TOOLS
                continue
            description = desc.schema.description or ""
            prefix = tool_name.split("_")[0] if "_" in tool_name else "tool"
            skills.append(
                new_agent_skill(
                    tool_name,
                    tool_name,
                    description[:200],
                    [prefix],
                )
            )
        # Only a real skill list is cached. The fallback below, and an empty result, must
        # stay retryable: the usual reason for either is that startup is still under way.
        if skills:
            _SKILLS_CACHE = list(skills)
        return skills
    except Exception:
        log.debug("Tool catalog not available yet, using fallback skills", exc_info=True)
        return [
            new_agent_skill(
                "general",
                "General Assistant",
                "Code editing, analysis, git operations, file management",
                ["general"],
            )
        ]


def _build_agent_card(settings: SettingsLike, host: str, port: int, identity_text: str) -> Any:
    """Build the card. ``identity_text`` is passed in, never read here.

    Required rather than defaulted: the I/O it replaces belongs off the event
    loop, and a caller that forgot it would silently publish an unnamed card —
    the failure this split exists to make impossible.
    """
    id_name, id_desc = _parse_identity(identity_text)
    name = settings.get("A2A_AGENT_NAME", "").strip() or id_name or "Ouroboros"
    description = settings.get("A2A_AGENT_DESCRIPTION", "").strip() or id_desc or "Personal AI agent"

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
        skills=_build_skills_from_registry(),
    )


async def _refresh_skills(card: Any) -> Any:
    live_skills = _build_skills_from_registry()
    if live_skills and live_skills[0].id != "general":
        try:
            card.skills = live_skills
        except Exception:
            del card.skills[:]
            card.skills.extend(live_skills)
    return card


def _build_starlette_app(
    agent_card: Any,
    handler: Any,
) -> Any:
    if _A2AStarletteApplication is not None:
        return _A2AStarletteApplication(
            agent_card=agent_card,
            http_handler=handler,
            card_modifier=_refresh_skills,
        ).build(
            agent_card_url="/.well-known/agent-card.json",
            rpc_url="/",
        )

    routes = []
    routes.extend(
        _create_agent_card_routes(
            agent_card=agent_card,
            card_modifier=_refresh_skills,
        )
    )
    routes.extend(
        _create_jsonrpc_routes(
            request_handler=handler,
            rpc_url="/",
            enable_v0_3_compat=True,
        )
    )
    return _Starlette(routes=routes)


async def _task_cleanup_loop(store: SQLiteA2ATaskStore, interval_sec: int = 3600) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            removed = await store.cleanup_expired()
            if removed:
                log.info("A2A task cleanup: removed %d expired tasks", removed)
        except Exception:
            log.warning("A2A task cleanup error", exc_info=True)


async def start_a2a_server(settings: SettingsLike) -> None:
    global _server, _cleanup_task

    if not _A2A_AVAILABLE:
        log.warning(
            "a2a-sdk is not installed — A2A server cannot start. "
            "Install with: pip install 'a2a-sdk[http-server]==1.0.3'"
        )
        return

    from ouroboros.app.config import DATA_DIR

    host = str(settings.get("A2A_HOST", "127.0.0.1")).strip()
    port = int(settings.get("A2A_PORT", 18800))
    max_concurrent = int(settings.get("A2A_MAX_CONCURRENT", 3))
    ttl_hours = int(settings.get("A2A_TASK_TTL_HOURS", 24))

    log.info("Starting A2A server on %s:%d", host, port)

    task_store = SQLiteA2ATaskStore(DATA_DIR, ttl_hours=ttl_hours)
    executor = OuroborosA2AExecutor(max_concurrent=max_concurrent, task_store=task_store)
    # Off the loop: the identity read is a runtime-DB round trip that also seeds
    # the row when it is absent, so it can block on a write transaction.
    identity_text = await asyncio.to_thread(_load_identity_text)
    agent_card = _build_agent_card(settings, host, port, identity_text)

    import inspect

    handler_kwargs: dict[str, Any] = {
        "agent_executor": executor,
        "task_store": task_store,
    }
    if "agent_card" in inspect.signature(_DefaultRequestHandler).parameters:
        handler_kwargs["agent_card"] = agent_card
    handler = _DefaultRequestHandler(**handler_kwargs)
    starlette_app = _build_starlette_app(agent_card, handler)

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
        if not local_cleanup_task.done():
            local_cleanup_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(local_cleanup_task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass


def stop_a2a_server() -> None:
    global _server, _cleanup_task
    if _server:
        _server.should_exit = True
        _server = None
    _cleanup_task = None
    log.info("A2A server shutdown requested")
