"""One remote A2A agent: discovery, the tool it publishes, and its task lifecycle.

Where an MCP call returns, an A2A task lives. It can stop three ways, and they mean different
things: **terminal** (completed / failed / canceled / rejected), **interrupted**
(``input-required`` / ``auth-required`` — neither running nor over, so polling stops there
too), or not at all, which is the call's deadline running out.

Protocol helpers come from :mod:`transport.a2a.proto` — the SDK-layout shim both
directions need.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, TypeVar, assert_never

import httpx

from infra.user_tokens.interface import UserTokenTargetKind
from new_core.identity import OwnerScope
from new_core.tools.loop_bridge import BRIDGE_GRACE_SEC
from new_core.tools.schema import ToolSchema
from ouroboros.egress.a2a import transports
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.auth.authenticators import authenticator_for, static_headers
from ouroboros.egress.auth.interfaces import EgressAuthRefused
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from ouroboros.tools.naming import cap_tool_name, sanitize_tool_name
from transport.a2a.proto import (
    get_message_text,
    is_terminal_task_state,
    new_user_text_message,
    task_state_name,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from a2a.client import Client, ClientCallContext
    from a2a.types.a2a_pb2 import AgentCard, Task

try:
    from a2a.types.a2a_pb2 import CancelTaskRequest, GetTaskRequest, SendMessageRequest
except ImportError:  # pragma: no cover - exercised only where the SDK is absent
    pass

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Names this boundary in the mint, the way `mcp` names the other outbound one.
_TRACE_SOURCE = "a2a-egress"

#: A peer can publish dozens of skills and nothing downstream caps a description.
DESCRIPTION_MAX_CHARS = 2000

#: Polling stretches out on a task that keeps running: a 5-minute deadline at the default
#: 2s interval would otherwise be 150 full round trips, each carrying the whole task.
POLL_BACKOFF_FACTOR = 1.5
POLL_INTERVAL_MAX_SEC = 15.0

#: The post-deadline cancel is courtesy, not the answer — it must not outlive the call.
CANCEL_ON_TIMEOUT_BUDGET_SEC = 10.0

#: One margin, spent by whoever waits on a call from outside it: the cancel that follows the
#: deadline, plus the cross-loop hop. Anything shorter cuts off a cancel still in flight.
BRIDGE_MARGIN_SEC = CANCEL_ON_TIMEOUT_BUDGET_SEC + BRIDGE_GRACE_SEC

_ABANDONED_RESOLUTION_CANCEL_INTERVAL_SEC = 0.1
_ABANDONED_RESOLUTION_CANCEL_BUDGET_SEC = 2.0

_INTERRUPTED_STATES = frozenset({"input-required", "auth-required"})

#: A send that was written but never answered. The remote agent may hold the work, so this is
#: not "not accepted" — see :func:`format_a2a_submission_unknown`. A connect or pool timeout is
#: deliberately absent: nothing was sent, so nothing was started.
_SUBMISSION_UNKNOWN_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout)
_REJECTED_STATUSES = frozenset({401, 403})


def tool_ceiling_sec(timeout_sec: float) -> int:
    """What a caller must allow for a call whose own deadline is ``timeout_sec``.

    The deadlines are strictly ordered, innermost first: the operation's deadline, then the
    cancel it sends once that expires, then the bridge's cross-loop wait, then this ceiling —
    a second wider still. The order is the point. What fires has to be the innermost deadline,
    because it is the only one that knows the remote ``task_id``; an outer wait that expired
    first would report a timeout with nothing to collect the work by.
    """
    return math.ceil(float(timeout_sec) + BRIDGE_MARGIN_SEC) + 1


def _causes(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _submission_unknown(exc: BaseException) -> bool:
    return any(isinstance(cause, _SUBMISSION_UNKNOWN_ERRORS) for cause in _causes(exc))


def _credentials_rejected(exc: BaseException) -> bool:
    return any(
        isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in _REJECTED_STATUSES
        for cause in _causes(exc)
    )


class _CallRefused(Exception):
    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result


class _DeadlinePassed(TimeoutError):
    pass


_TOOL_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "What to ask the remote agent, in plain language. It has no other input.",
        },
        "wait": {
            "type": "boolean",
            "description": (
                "Wait for the remote agent to finish (default true). Pass false for work "
                "expected to take minutes: the call returns a task_id immediately and the "
                "result is collected later with a2a_task_status."
            ),
        },
        "task_id": {
            "type": "string",
            "description": (
                "Continue an existing remote task instead of starting a new one — pass the "
                "task_id exactly as an earlier result reported it, e.g. to answer a question the agent asked."
            ),
        },
    },
    "required": ["message"],
}


def build_a2a_tool_name(agent_id: str) -> str:
    """``a2a__{agent}`` — one name per agent, because the protocol cannot address a skill."""
    return cap_tool_name(f"a2a__{sanitize_tool_name(agent_id)}")


def format_a2a_error(agent_id: str, detail: object) -> str:
    return f"⚠️ A2A_ERROR ({agent_id}): {detail}"


def format_a2a_timeout(agent_id: str, detail: object) -> str:
    return f"⚠️ A2A_TIMEOUT ({agent_id}): {detail}"


def format_a2a_submission_unknown(agent_id: str, detail: object, *, task_id: str = "") -> str:
    """The send got no answer, which says nothing about whether the work started.

    Deliberately not ``A2A_TIMEOUT``: that one promises a ``task_id`` the result can be
    collected by, and there is none here. Resending is what turns one request into two remote
    tasks, so the result says not to.
    """
    if task_id:
        return (
            f"⚠️ A2A_SUBMISSION_UNKNOWN ({agent_id}): {detail} — sent against task {task_id}, which may have "
            f"accepted it. Do not resend: read the task first with "
            f'a2a_task_status(agent_id="{agent_id}", task_id="{task_id}").'
        )
    return (
        f"⚠️ A2A_SUBMISSION_UNKNOWN ({agent_id}): {detail} — no task id came back, so whether the request "
        "started a remote task is unknown. Do not resend it blindly; say the outcome is unknown and, if the "
        "work matters, have the operator check that agent."
    )


def format_a2a_input_required(agent_id: str, question: str, task_id: str) -> str:
    """Deliberately not a failure prefix: the task is alive and the question is the result."""
    asked = question.strip() or "(the remote agent asked for more input but sent no question text)"
    return (
        f"⚠️ A2A_INPUT_REQUIRED ({agent_id}): {asked}\n"
        f"task_id={task_id} — answer by calling this tool again with that task_id."
    )


def _artifact_text(task: Task) -> str:
    return "\n".join(part.text for artifact in task.artifacts for part in artifact.parts if part.text)


def _status_text(task: Task) -> str:
    if not task.status.HasField("message"):
        return ""
    return get_message_text(task.status.message).strip()


def _normalized_state(task: Task) -> str:
    return task_state_name(task.status.state).replace("_", "-")


class A2aRemoteAgentConnection:
    """Owns one remote agent's HTTP client, SDK client, discovered card, and tool schema."""

    def __init__(self, entry: A2aRemoteAgentEntry, dependencies: A2aEgressDependencies) -> None:
        self.entry = entry
        self.tool_name = build_a2a_tool_name(entry.id)
        self.card: AgentCard | None = None
        self.card_available = False
        self.schema = self._build_schema()
        self._authenticator = authenticator_for(entry, UserTokenTargetKind.A2A_AGENT, dependencies.auth)
        self._task_handles = dependencies.task_handles
        self._abandoned_resolutions: set[asyncio.Task[dict[str, str]]] = set()
        self._http_client: httpx.AsyncClient | None = None
        self._client: Client | None = None

    # --- lifecycle ----------------------------------------------------------------------

    async def connect(self) -> None:
        """Discover the card and build the client. A missing card degrades, it does not raise.

        Wrapped in a scope of its own because discovery has no caller: it runs from the app
        lifespan, where nothing is active, so the request hook on the client below had nothing
        to stamp and the card fetch — the one outbound hop this process makes before any task
        exists — went out uncorrelated. A peer that answers it with a 503 could then not be
        matched against its own logs, which is the whole point of carrying the identifier.

        Minted rather than inherited, for the reason ``ouroboros/mcp/connection.py`` gives:
        the client built here outlives this scope, and its per-request hook reads whatever is
        active at send time, so a later ``call()`` still carries its own task's identifier
        rather than this one.
        """
        with aef_tracing.trace_scope(aef_tracing.accept_trace_id("", source=_TRACE_SOURCE)):
            await self._connect_scoped()

    async def _connect_scoped(self) -> None:
        self._http_client = transports.build_http_client(self.entry, self._authenticator.forwarded_headers)
        try:
            try:
                self.card = await asyncio.wait_for(
                    transports.resolve_agent_card(self._http_client, self.entry.url, static_headers(self.entry.auth)),
                    timeout=transports.CARD_CONNECT_TIMEOUT_SEC,
                )
                self.card_available = True
                self.schema = self._build_schema()
            except Exception as exc:
                log.warning(
                    "A2A agent %s: agent card unreachable at %s (%s); offering a bare message tool",
                    self.entry.id,
                    self.entry.url,
                    exc,
                )
                self.card = transports.fallback_agent_card(self.entry.url)
                self.card_available = False
            self.card = transports.same_origin_card(self.card, self.entry.url)
            self._client = transports.build_a2a_client(self.card, self._http_client)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:
                log.debug("A2A agent %s: client close failed", self.entry.id, exc_info=True)
        http_client, self._http_client = self._http_client, None
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                log.debug("A2A agent %s: HTTP client close failed", self.entry.id, exc_info=True)
        deadline = time.monotonic() + _ABANDONED_RESOLUTION_CANCEL_BUDGET_SEC
        while abandoned := set(self._abandoned_resolutions):
            for resolving in abandoned:
                resolving.cancel()
            await asyncio.wait(abandoned, timeout=_ABANDONED_RESOLUTION_CANCEL_INTERVAL_SEC)
            if time.monotonic() >= deadline:
                break
        unstopped = len(self._abandoned_resolutions)
        if unstopped:
            log.warning(
                "A2A agent %s: %d credential resolutions ignored cancels for %.1fs; closing without them",
                self.entry.id,
                unstopped,
                _ABANDONED_RESOLUTION_CANCEL_BUDGET_SEC,
            )

    # --- the published tool -------------------------------------------------------------

    def _build_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.tool_name,
            description=self._build_description(),
            parameters=dict(_TOOL_PARAMETERS),
        )

    def _build_description(self) -> str:
        """Published skills lead, because ``list_available_tools`` prints only the first 120
        characters and that line is where the model picks an agent."""
        label = (self.entry.name or "").strip() or self.entry.id
        card = self.card
        if not self.card_available or card is None:
            text = (
                f"[A2A {self.entry.id}] Remote agent {label}: its agent card could not be read, "
                f"so its skills are unknown. Send a request in plain language to {self.entry.url}."
            )
            return text[:DESCRIPTION_MAX_CHARS]
        parts = [f"[A2A {self.entry.id}] Delegate to {card.name.strip() or label}."]
        skills = self._featured_skill_lines(card)
        if skills:
            parts.append("Skills: " + "; ".join(skills) + ".")
        if card.description.strip():
            parts.append(card.description.strip())
        return " ".join(parts)[:DESCRIPTION_MAX_CHARS]

    def _featured_skill_lines(self, card: AgentCard) -> list[str]:
        """``featured_skills`` selects what is mentioned and gates nothing."""
        wanted = {str(name).strip() for name in self.entry.featured_skills if str(name).strip()}
        lines: list[str] = []
        for skill in card.skills:
            name = skill.name or skill.id
            if wanted and skill.id not in wanted and name not in wanted:
                continue
            lines.append(f"{name} — {skill.description.strip()}" if skill.description.strip() else name)
        return lines

    # --- calls --------------------------------------------------------------------------

    async def call(
        self, scope: OwnerScope, message: str, *, timeout_sec: float, wait: bool = True, task_id: str = ""
    ) -> str:
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")

        text = str(message or "").strip()
        if not text:
            return format_a2a_error(self.entry.id, "message is required and must not be empty")

        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        continued = str(task_id or "").strip()
        try:
            remote_task_id = self._open_handle(scope, continued) if continued else ""
            context = await self._call_context(scope, deadline, timeout_sec)
        except _CallRefused as refused:
            return refused.result
        try:
            responses = await self._within(self._send(context, text, remote_task_id), deadline)
        except _DeadlinePassed:
            return self._unresolved(timeout_sec).result
        except asyncio.TimeoutError:
            return format_a2a_submission_unknown(
                self.entry.id, f"no answer to the send within {timeout_sec:g}s", task_id=continued
            )
        except Exception as exc:
            log.warning("A2A send to %s failed: %s", self.entry.id, exc)
            if _credentials_rejected(exc):
                return self._format_rejected("the message")
            if _submission_unknown(exc):
                return format_a2a_submission_unknown(self.entry.id, exc, task_id=continued)
            if continued:
                return await self._explain_failed_continuation(scope, context, remote_task_id, exc, deadline)
            return format_a2a_error(self.entry.id, exc)

        if not responses:
            return format_a2a_error(self.entry.id, "the remote agent returned no response")
        response = responses[-1]

        if response.HasField("message"):
            return get_message_text(response.message).strip() or "(the remote agent replied with an empty message)"
        if not response.HasField("task"):
            return format_a2a_error(self.entry.id, "the remote agent returned neither a task nor a message")

        if not wait:
            return self._format_submitted(scope, response.task)
        return await self._await_task(
            scope, context, response.task, deadline=deadline, timeout_sec=timeout_sec, cancel_on_timeout=True
        )

    async def status(self, scope: OwnerScope, task_id: str, *, timeout_sec: float, wait_sec: float = 0.0) -> str:
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")
        requested = str(task_id or "").strip()
        if not requested:
            return format_a2a_error(self.entry.id, "task_id is required")
        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        try:
            remote_task_id = self._open_handle(scope, requested)
            context = await self._call_context(scope, deadline, timeout_sec)
        except _CallRefused as refused:
            return refused.result
        try:
            task = await self._within(self._get_task(context, remote_task_id), deadline)
        except _DeadlinePassed:
            return self._unresolved(timeout_sec).result
        except asyncio.TimeoutError:
            return format_a2a_timeout(self.entry.id, f"could not read task {requested} within {timeout_sec:g}s")
        except Exception as exc:
            if _credentials_rejected(exc):
                return self._format_rejected(f"reading task {requested}")
            return format_a2a_error(self.entry.id, f"could not read task {requested}: {exc}")
        if wait_sec <= 0:
            return self._format_state(scope, task)
        wait_deadline = min(deadline, time.monotonic() + max(float(wait_sec), 0.0))
        return await self._await_task(
            scope, context, task, deadline=wait_deadline, timeout_sec=wait_sec, cancel_on_timeout=False
        )

    async def cancel(self, scope: OwnerScope, task_id: str, *, timeout_sec: float) -> str:
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")
        requested = str(task_id or "").strip()
        if not requested:
            return format_a2a_error(self.entry.id, "task_id is required")
        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        try:
            remote_task_id = self._open_handle(scope, requested)
            context = await self._call_context(scope, deadline, timeout_sec)
        except _CallRefused as refused:
            return refused.result
        try:
            task = await self._within(self._cancel_task(context, remote_task_id), deadline)
        except _DeadlinePassed:
            return self._unresolved(timeout_sec).result
        except asyncio.TimeoutError:
            return format_a2a_timeout(self.entry.id, f"could not cancel task {requested} within {timeout_sec:g}s")
        except Exception as exc:
            if _credentials_rejected(exc):
                return self._format_rejected(f"cancelling task {requested}")
            return format_a2a_error(self.entry.id, f"could not cancel task {requested}: {exc}")
        return (
            f"A2A task {self._handle(scope, task.id)} on {self.entry.id} is now {task_state_name(task.status.state)}."
        )

    # --- private ------------------------------------------------------------------------

    async def _within(self, coro: Coroutine[Any, Any, T], deadline: float) -> T:
        """Run ``coro`` inside what is left of the call's deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            coro.close()
            raise _DeadlinePassed
        return await asyncio.wait_for(coro, timeout=remaining)

    def _handle(self, scope: OwnerScope, task_id: str) -> str:
        return self._task_handles.seal(scope, self.entry.id, task_id)

    def _open_handle(self, scope: OwnerScope, handle: str) -> str:
        task_id = self._task_handles.open(scope, self.entry.id, handle)
        if task_id is None:
            if not self._task_handles.is_handle_format(handle):
                raise _CallRefused(
                    format_a2a_error(
                        self.entry.id,
                        f"task_id {handle!r} is not a sealed handle; a task started before the handle "
                        "cut-over cannot be followed, cancelled or continued — start a new task and "
                        "pass task_id exactly as the result that started it reported it",
                    )
                )
            raise _CallRefused(
                format_a2a_error(
                    self.entry.id,
                    f"task_id {handle!r} was not started by this owner and agent with {self.entry.id}; "
                    "pass task_id exactly as the result that started the task reported it",
                )
            )
        return task_id

    async def _call_context(self, scope: OwnerScope, deadline: float, timeout_sec: float) -> ClientCallContext:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._unresolved(timeout_sec)
        resolving = asyncio.create_task(self._authenticator.request_headers(scope))
        try:
            done, _ = await asyncio.wait({resolving}, timeout=remaining)
        except asyncio.CancelledError:
            self._abandon(resolving)
            raise
        if not done:
            self._abandon(resolving)
            raise self._unresolved(timeout_sec)
        try:
            headers = resolving.result()
        except EgressAuthRefused as refusal:
            log.warning("A2A call to %s was not authorized: %s", self.entry.id, refusal.reason)
            raise _CallRefused(format_a2a_error(self.entry.id, f"cannot be authorized: {refusal.reason}")) from refusal
        return transports.call_context(headers)

    def _unresolved(self, timeout_sec: float) -> _CallRefused:
        return _CallRefused(
            format_a2a_error(
                self.entry.id, f"the credentials were not resolved within {timeout_sec:g}s, so nothing was sent"
            )
        )

    def _abandon(self, resolving: asyncio.Task[dict[str, str]]) -> None:
        resolving.cancel()
        self._abandoned_resolutions.add(resolving)
        resolving.add_done_callback(self._abandoned_resolutions.discard)
        resolving.add_done_callback(self._collect_abandoned)

    def _collect_abandoned(self, resolving: asyncio.Task[dict[str, str]]) -> None:
        if resolving.cancelled():
            return
        error = resolving.exception()
        if error is not None:
            log.warning(
                "A2A agent %s: credentials abandoned at the deadline failed afterwards (%s)",
                self.entry.id,
                type(error).__name__,
            )

    def _credential_hint(self) -> str:
        auth = self.entry.auth
        match auth:
            case NoAuth() | MTLSAuth():
                return (
                    "this deployment sends the agent no credential, so an operator must change its entry in "
                    "security_config/a2a.py"
                )
            case UserHeadersAuth():
                return "it did not accept the credentials of the user's browser session, so the user must sign in again"
            case UserTokenAuth():
                return (
                    "it did not accept the token sent for this owner and agent, so the owner can store a new "
                    f"personal token for A2A agent {self.entry.id} in owner settings, or an operator must fix the "
                    "static token in security_config/a2a.py"
                )
        assert_never(auth)

    def _format_rejected(self, what: str) -> str:
        return format_a2a_error(self.entry.id, f"the remote agent did not authorize {what} — {self._credential_hint()}")

    async def _send(self, context: ClientCallContext, text: str, task_id: str) -> list[Any]:
        assert self._client is not None
        request = SendMessageRequest(message=new_user_text_message(text, task_id=task_id or None))
        return [response async for response in self._client.send_message(request, context=context)]

    async def _get_task(self, context: ClientCallContext, task_id: str) -> Task:
        assert self._client is not None
        return await self._client.get_task(GetTaskRequest(id=task_id, history_length=0), context=context)

    async def _cancel_task(self, context: ClientCallContext, task_id: str) -> Task:
        assert self._client is not None
        return await self._client.cancel_task(CancelTaskRequest(id=task_id), context=context)

    async def _explain_failed_continuation(
        self, scope: OwnerScope, context: ClientCallContext, task_id: str, exc: Exception, deadline: float
    ) -> str:
        handle = self._handle(scope, task_id)
        try:
            task = await self._within(self._get_task(context, task_id), deadline)
        except Exception:
            return format_a2a_error(self.entry.id, f"could not continue task {handle}: {exc}")
        if is_terminal_task_state(task.status.state):
            return format_a2a_error(
                self.entry.id,
                f"task {handle} has already ended in state '{task_state_name(task.status.state)}' "
                "and cannot be continued; send a new request without task_id",
            )
        return format_a2a_error(self.entry.id, exc)

    async def _await_task(
        self,
        scope: OwnerScope,
        context: ClientCallContext,
        task: Task,
        *,
        deadline: float,
        timeout_sec: float,
        cancel_on_timeout: bool,
    ) -> str:
        current = task
        interval = max(float(self.entry.poll_interval_sec), 0.0)
        while True:
            if is_terminal_task_state(current.status.state):
                return self._format_terminal(scope, current)
            state = _normalized_state(current)
            if state in _INTERRUPTED_STATES:
                return self._format_interrupted(scope, current, state)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not cancel_on_timeout:
                    return self._format_state(scope, current)
                return await self._timed_out(scope, context, current, timeout_sec)
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * POLL_BACKOFF_FACTOR, POLL_INTERVAL_MAX_SEC)
            try:
                current = await self._within(self._get_task(context, current.id), deadline)
            except asyncio.TimeoutError:
                if not cancel_on_timeout:
                    return self._format_state(scope, current)
                return await self._timed_out(scope, context, current, timeout_sec)
            except Exception as exc:
                log.warning("A2A poll of %s task %s failed: %s", self.entry.id, current.id, exc)
                handle = self._handle(scope, current.id)
                if _credentials_rejected(exc):
                    return self._format_rejected(f"reading task {handle}")
                return format_a2a_error(self.entry.id, f"lost track of task {handle}: {exc}")

    async def _timed_out(self, scope: OwnerScope, context: ClientCallContext, task: Task, timeout_sec: float) -> str:
        try:
            await asyncio.wait_for(self._cancel_task(context, task.id), timeout=CANCEL_ON_TIMEOUT_BUDGET_SEC)
            cancelled = "cancel requested"
        except Exception as exc:
            log.warning("A2A cancel of %s task %s failed: %s", self.entry.id, task.id, exc)
            if _credentials_rejected(exc):
                cancelled = f"the remote agent did not authorize the cancel — {self._credential_hint()}"
            else:
                cancelled = f"cancel request failed: {exc}"
        return format_a2a_timeout(
            self.entry.id, f"no result within {timeout_sec:g}s ({cancelled}); task_id={self._handle(scope, task.id)}"
        )

    def _format_submitted(self, scope: OwnerScope, task: Task) -> str:
        handle = self._handle(scope, task.id)
        return (
            f"A2A task submitted to {self.entry.id}: task_id={handle} "
            f"state={task_state_name(task.status.state)}.\n"
            f"The remote agent is working. Collect the result with "
            f'a2a_task_status(agent_id="{self.entry.id}", task_id="{handle}").'
        )

    def _format_state(self, scope: OwnerScope, task: Task) -> str:
        if is_terminal_task_state(task.status.state):
            return self._format_terminal(scope, task)
        line = f"A2A task {self._handle(scope, task.id)} on {self.entry.id} is {task_state_name(task.status.state)}."
        note = _status_text(task)
        return f"{line}\n{note}" if note else line

    def _format_terminal(self, scope: OwnerScope, task: Task) -> str:
        state = task_state_name(task.status.state)
        handle = self._handle(scope, task.id)
        if state == "completed":
            body = _artifact_text(task) or _status_text(task)
            return body or f"A2A task {handle} on {self.entry.id} completed with no output."
        detail = _status_text(task) or _artifact_text(task)
        message = f"remote task {handle} ended in state '{state}'"
        return format_a2a_error(self.entry.id, f"{message}: {detail}" if detail else message)

    def _format_interrupted(self, scope: OwnerScope, task: Task, state: str) -> str:
        handle = self._handle(scope, task.id)
        if state == "auth-required":
            return format_a2a_error(
                self.entry.id,
                f"the remote agent requires credentials (auth-required) — {self._credential_hint()}; task_id={handle}",
            )
        return format_a2a_input_required(self.entry.id, _status_text(task) or _artifact_text(task), handle)


__all__ = [
    "BRIDGE_MARGIN_SEC",
    "CANCEL_ON_TIMEOUT_BUDGET_SEC",
    "DESCRIPTION_MAX_CHARS",
    "A2aRemoteAgentConnection",
    "build_a2a_tool_name",
    "format_a2a_error",
    "format_a2a_input_required",
    "format_a2a_submission_unknown",
    "format_a2a_timeout",
    "tool_ceiling_sec",
]
