"""The scripted remote agent: the body the in-process fake and the local servers both run.

Real SDK server, real ``DefaultRequestHandler``, real task lifecycle — only the agent body is
a script, selected by the first word of the message (see ``ScriptedExecutor.execute`` below).

The bearer machinery lives here rather than in the product: this agent's own ingress accepts
anyone, and what these servers stand in for is somebody else's guarded agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from ouroboros.ingress.a2a_proto import (
    build_agent_card_proto,
    get_message_text,
    new_agent_skill,
    new_task_event,
    new_text_artifact_update_event,
    new_text_status_update_event,
    new_user_text_message,
    task_state_value,
)

DEFAULT_NAME = "Fake Remote Agent"
DEFAULT_DESCRIPTION = "A remote agent that exists only in this test."

#: The name a card gives its bearer scheme — what a caller reads off a guarded agent's card.
BEARER_SCHEME_NAME = "bearerAuth"

#: What the scripted body can be asked to do, as an agent card advertises it.
DEFAULT_SKILLS: list[tuple[str, str]] = [
    ("echo", "Answers with the request text as an artifact"),
    ("slow", "Works for the number of seconds in 'slow:<seconds>' before answering"),
    ("ask", "Stops and asks a clarifying question instead of answering"),
]


@dataclass
class ScriptedAgent:
    """One scripted agent: its ASGI app, its card, and a record of what it was asked."""

    agent_id: str
    url: str
    app: Any
    card: Any
    received: list[str] = field(default_factory=list)
    #: Task ids whose body ran to completion — the evidence a cancel actually stopped one.
    completed: list[str] = field(default_factory=list)


class ScriptedExecutor:
    """The agent body. Duck-typed: the SDK only ever calls ``execute`` and ``cancel``."""

    def __init__(self, agent: ScriptedAgent) -> None:
        self._agent = agent
        self._cancelled: set[str] = set()

    async def execute(self, context: Any, event_queue: Any) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        text = get_message_text(context.message).strip() if context.message is not None else ""
        self._agent.received.append(text)

        if text.startswith("message-only"):
            # A reply with no task behind it — the other half of the send response oneof.
            await event_queue.enqueue_event(new_user_text_message(f"direct reply: {text}"))
            return

        continuing = getattr(context, "current_task", None) is not None
        if not continuing:
            await event_queue.enqueue_event(new_task_event(task_id, context_id, "submitted"))

        if text.startswith("auth"):
            await self._status(event_queue, task_id, context_id, "auth-required", "token rejected")
            return

        if text.startswith("ask") and not continuing:
            await self._status(
                event_queue,
                task_id,
                context_id,
                "input-required",
                "Which repository should I look at?",
            )
            return

        await self._status(event_queue, task_id, context_id, "working", final=False)

        if text.startswith("boom"):
            await self._status(event_queue, task_id, context_id, "failed", "the remote agent broke")
            return

        if text.startswith("reject"):
            await self._status(event_queue, task_id, context_id, "rejected", "not something I do")
            return

        if text.startswith("slow:"):
            try:
                delay = float(text.split(":", 1)[1].split()[0])
            except (IndexError, ValueError):
                delay = 1.0
            await asyncio.sleep(delay)

        if task_id in self._cancelled:
            return
        self._agent.completed.append(task_id)

        if text.startswith("quiet"):
            # Terminal with nothing to show: the caller has to say so rather than print blank.
            await self._status(event_queue, task_id, context_id, "completed")
            return

        await self._artifact(event_queue, task_id, context_id, f"answer: {text}")
        await self._status(event_queue, task_id, context_id, "completed")

    async def cancel(self, context: Any, event_queue: Any) -> None:
        task_id = context.task_id or ""
        self._cancelled.add(task_id)
        await self._status(event_queue, task_id, context.context_id or "", "canceled")

    @staticmethod
    async def _status(event_queue, task_id, context_id, state, text="", *, final=None) -> None:
        await event_queue.enqueue_event(
            new_text_status_update_event(task_id, context_id, task_state_value(state), text, final=final)
        )

    @staticmethod
    async def _artifact(event_queue, task_id, context_id, text) -> None:
        await event_queue.enqueue_event(
            new_text_artifact_update_event(task_id, context_id, name="response", text=text, last_chunk=True)
        )


def _declare_bearer_scheme(card: Any) -> None:
    """Advertise the bearer requirement on an already-built card.

    Merged into the card rather than passed to the builder: only a remote agent demands a
    credential of us, so the option has no place in the card this agent publishes.
    """
    ParseDict(
        {
            "securitySchemes": {
                BEARER_SCHEME_NAME: {
                    "httpAuthSecurityScheme": {
                        "scheme": "bearer",
                        "description": "Access token issued by this agent's operator.",
                    }
                }
            },
            "securityRequirements": [{"schemes": {BEARER_SCHEME_NAME: {"list": []}}}],
        },
        card,
    )


def _extract_bearer_token(headers: Mapping[str, str]) -> str:
    raw = str(headers.get("authorization") or "")
    prefix = "bearer "
    if raw.lower().startswith(prefix):
        return raw[len(prefix) :].strip()
    return raw.strip()


class BearerAuthMiddleware:
    """Refuse JSON-RPC that does not carry the expected bearer.

    The agent card stays public, because discovery is what tells a caller which credential to
    present.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or "/.well-known/" in str(scope.get("path") or ""):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        if _extract_bearer_token(headers) != self.token:
            response = JSONResponse(
                {"error": "unauthorized", "detail": "A valid bearer token is required for A2A requests."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_scripted_agent(
    agent_id: str = "fake",
    *,
    base_url: str,
    mount: str = "",
    name: str = DEFAULT_NAME,
    description: str = DEFAULT_DESCRIPTION,
    skills: list[tuple[str, str]] | None = None,
    require_token: str = "",
    serve_card: bool = True,
) -> ScriptedAgent:
    """A scripted agent ready to be mounted in-process or served over a port.

    The agent answers at ``{base_url}{mount}``: its card under that path's ``/.well-known/``
    and JSON-RPC at the path itself. ``mount`` is empty for an agent alone on a port, which is
    the shape a real deployment has; the in-process fake gives each agent a path instead so
    several can share one host. ``skills`` are ``(name, description)`` pairs, ``require_token``
    is the bearer JSON-RPC will demand, and ``serve_card=False`` drops the card route.
    """
    url = f"{base_url.rstrip('/')}{mount}"
    card = build_agent_card_proto(
        name=name,
        description=description,
        version="1.0.0",
        service_url=url + "/",
        skills=[
            new_agent_skill(skill_name, skill_name, skill_desc, ["fake"]) for skill_name, skill_desc in skills or []
        ],
    )
    if require_token:
        _declare_bearer_scheme(card)
    agent = ScriptedAgent(agent_id=agent_id, url=url, app=None, card=card)
    handler = DefaultRequestHandler(
        agent_executor=ScriptedExecutor(agent),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    # v0.3 compat as the real ingress enables it, so a hand-written `message/send` probe
    # reaches a fake exactly as it reaches the product.
    routes = list(create_jsonrpc_routes(request_handler=handler, rpc_url=f"{mount}/", enable_v0_3_compat=True))
    if serve_card:
        routes = [
            *create_agent_card_routes(agent_card=card, card_url=f"{mount}/.well-known/agent-card.json"),
            *routes,
        ]
    app: Any = Starlette(routes=routes)
    agent.app = BearerAuthMiddleware(app, require_token) if require_token else app
    return agent


__all__ = [
    "BEARER_SCHEME_NAME",
    "DEFAULT_DESCRIPTION",
    "DEFAULT_NAME",
    "DEFAULT_SKILLS",
    "BearerAuthMiddleware",
    "ScriptedAgent",
    "ScriptedExecutor",
    "build_scripted_agent",
]
