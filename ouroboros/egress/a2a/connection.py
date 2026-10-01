"""One remote A2A agent: discovery, the tool it publishes, and its task lifecycle.

Where an MCP call returns, an A2A task lives. It can stop three ways, and they mean different
things: **terminal** (completed / failed / canceled / rejected), **interrupted**
(``input-required`` / ``auth-required`` — neither running nor over, so polling stops there
too), or not at all, which is the call's deadline running out.

Protocol helpers come from :mod:`ouroboros.ingress.a2a_proto` — the SDK-layout shim both
directions need.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from new_core.tools.loop_bridge import BRIDGE_GRACE_SEC
from new_core.tools.schema import ToolSchema
from ouroboros.egress.a2a import transports
from ouroboros.ingress.a2a_proto import (
    get_message_text,
    is_terminal_task_state,
    new_user_text_message,
    task_state_name,
)
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from ouroboros.tools.naming import cap_tool_name, sanitize_tool_name

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from a2a.client import Client
    from a2a.types.a2a_pb2 import AgentCard, Task

try:
    from a2a.types.a2a_pb2 import CancelTaskRequest, GetTaskRequest, SendMessageRequest
except ImportError:  # pragma: no cover - exercised only where the SDK is absent
    pass

log = logging.getLogger(__name__)

T = TypeVar("T")

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

_INTERRUPTED_STATES = frozenset({"input-required", "auth-required"})

#: A send that was written but never answered. The remote agent may hold the work, so this is
#: not "not accepted" — see :func:`format_a2a_submission_unknown`. A connect or pool timeout is
#: deliberately absent: nothing was sent, so nothing was started.
_SUBMISSION_UNKNOWN_ERRORS = (httpx.ReadTimeout, httpx.WriteTimeout)


def tool_ceiling_sec(timeout_sec: float) -> int:
    """What a caller must allow for a call whose own deadline is ``timeout_sec``.

    The deadlines are strictly ordered, innermost first: the operation's deadline, then the
    cancel it sends once that expires, then the bridge's cross-loop wait, then this ceiling —
    a second wider still. The order is the point. What fires has to be the innermost deadline,
    because it is the only one that knows the remote ``task_id``; an outer wait that expired
    first would report a timeout with nothing to collect the work by.
    """
    return math.ceil(float(timeout_sec) + BRIDGE_MARGIN_SEC) + 1


def _submission_unknown(exc: BaseException) -> bool:
    """Walks the cause chain, because the SDK wraps transport errors in its own."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _SUBMISSION_UNKNOWN_ERRORS):
            return True
        current = current.__cause__ or current.__context__
    return False


#: Card-independent: the protocol offers the same three inputs whether or not discovery
#: worked, and a degraded schema would advertise a task id the model could never send back.
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
                "task_id from an earlier call, e.g. to answer a question the agent asked."
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

    def __init__(self, entry: A2aRemoteAgentEntry) -> None:
        self.entry = entry
        self.tool_name = build_a2a_tool_name(entry.id)
        self.card: AgentCard | None = None
        #: False when the card could not be fetched: the tool still works, with a bare schema.
        self.card_available = False
        self.schema = self._build_schema()
        self._http_client: httpx.AsyncClient | None = None
        self._client: Client | None = None

    # --- lifecycle ----------------------------------------------------------------------

    async def connect(self) -> None:
        """Discover the card and build the client. A missing card degrades, it does not raise."""
        self._http_client = transports.build_http_client(self.entry)
        try:
            try:
                self.card = await asyncio.wait_for(
                    transports.resolve_agent_card(self._http_client, self.entry.url),
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
            self._client = transports.build_a2a_client(self.card, self._http_client)
        except BaseException:
            # Everything after the client is constructed can still raise — a missing SDK, a
            # card publishing no transport this build speaks. Nothing else holds a reference.
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

    async def call(self, message: str, *, timeout_sec: float, wait: bool = True, task_id: str = "") -> str:
        """Send a message — as a new task, or against an existing one — and answer for it.

        One deadline covers the whole call, discovery of a continued task included, and it is
        the innermost of the four (:func:`tool_ceiling_sec`): it has to be what expires first,
        because it is the only one holding the task id.
        """
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")

        text = str(message or "").strip()
        if not text:
            return format_a2a_error(self.entry.id, "message is required and must not be empty")

        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        continued = str(task_id or "").strip()
        try:
            responses = await self._within(self._send(text, continued), deadline)
        except asyncio.TimeoutError:
            # Not "the agent did not accept it": silence is not a refusal. The request was
            # written to a live connection and the far side may be running it already.
            return format_a2a_submission_unknown(
                self.entry.id, f"no answer to the send within {timeout_sec:g}s", task_id=continued
            )
        except Exception as exc:
            log.warning("A2A send to %s failed: %s", self.entry.id, exc)
            if _submission_unknown(exc):
                return format_a2a_submission_unknown(self.entry.id, exc, task_id=continued)
            if continued:
                return await self._explain_failed_continuation(continued, exc, deadline)
            return format_a2a_error(self.entry.id, exc)

        if not responses:
            return format_a2a_error(self.entry.id, "the remote agent returned no response")
        response = responses[-1]

        if response.HasField("message"):
            # Answered outright: there is no task id to report, and none to invent.
            return get_message_text(response.message).strip() or "(the remote agent replied with an empty message)"
        if not response.HasField("task"):
            return format_a2a_error(self.entry.id, "the remote agent returned neither a task nor a message")

        if not wait:
            return self._format_submitted(response.task)
        return await self._await_task(response.task, deadline=deadline, timeout_sec=timeout_sec)

    async def status(self, task_id: str, *, timeout_sec: float, wait_sec: float = 0.0) -> str:
        """Read a remote task, optionally waiting a bounded time for it to move."""
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")
        requested = str(task_id or "").strip()
        if not requested:
            return format_a2a_error(self.entry.id, "task_id is required")
        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        try:
            task = await self._within(self._get_task(requested), deadline)
        except asyncio.TimeoutError:
            return format_a2a_timeout(self.entry.id, f"could not read task {requested} within {timeout_sec:g}s")
        except Exception as exc:
            return format_a2a_error(self.entry.id, f"could not read task {requested}: {exc}")
        if wait_sec <= 0:
            return self._format_state(task)
        wait_deadline = min(deadline, time.monotonic() + max(float(wait_sec), 0.0))
        return await self._await_task(task, deadline=wait_deadline, timeout_sec=wait_sec, cancel_on_timeout=False)

    async def cancel(self, task_id: str, *, timeout_sec: float) -> str:
        if self._client is None:
            return format_a2a_error(self.entry.id, "the agent is not connected")
        requested = str(task_id or "").strip()
        if not requested:
            return format_a2a_error(self.entry.id, "task_id is required")
        deadline = time.monotonic() + max(float(timeout_sec), 0.0)
        try:
            task = await self._within(self._cancel_task(requested), deadline)
        except asyncio.TimeoutError:
            return format_a2a_timeout(self.entry.id, f"could not cancel task {requested} within {timeout_sec:g}s")
        except Exception as exc:
            return format_a2a_error(self.entry.id, f"could not cancel task {requested}: {exc}")
        return f"A2A task {requested} on {self.entry.id} is now {task_state_name(task.status.state)}."

    # --- private ------------------------------------------------------------------------

    async def _within(self, coro: Coroutine[Any, Any, T], deadline: float) -> T:
        """Run ``coro`` inside what is left of the call's deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            coro.close()
            raise asyncio.TimeoutError
        return await asyncio.wait_for(coro, timeout=remaining)

    async def _send(self, text: str, task_id: str) -> list[Any]:
        assert self._client is not None
        request = SendMessageRequest(message=new_user_text_message(text, task_id=task_id or None))
        return [response async for response in self._client.send_message(request)]

    async def _get_task(self, task_id: str) -> Task:
        """``history_length=0`` — nothing here reads the transcript, and it grows with the task."""
        assert self._client is not None
        return await self._client.get_task(GetTaskRequest(id=task_id, history_length=0))

    async def _cancel_task(self, task_id: str) -> Task:
        assert self._client is not None
        return await self._client.cancel_task(CancelTaskRequest(id=task_id))

    async def _explain_failed_continuation(self, task_id: str, exc: Exception, deadline: float) -> str:
        """Say plainly that a task has ended, rather than letting a protocol error through.

        Asked only once the send has already failed: checking up front would cost every
        continuation an extra round trip to learn what the send is about to say anyway.
        """
        try:
            task = await self._within(self._get_task(task_id), deadline)
        except Exception:
            return format_a2a_error(self.entry.id, f"could not continue task {task_id}: {exc}")
        if is_terminal_task_state(task.status.state):
            return format_a2a_error(
                self.entry.id,
                f"task {task_id} has already ended in state '{task_state_name(task.status.state)}' "
                "and cannot be continued; send a new request without task_id",
            )
        return format_a2a_error(self.entry.id, exc)

    async def _await_task(
        self,
        task: Task,
        *,
        deadline: float,
        timeout_sec: float,
        cancel_on_timeout: bool = True,
    ) -> str:
        current = task
        interval = max(float(self.entry.poll_interval_sec), 0.0)
        while True:
            if is_terminal_task_state(current.status.state):
                return self._format_terminal(current)
            state = _normalized_state(current)
            if state in _INTERRUPTED_STATES:
                return self._format_interrupted(current, state)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not cancel_on_timeout:
                    return self._format_state(current)
                return await self._timed_out(current, timeout_sec)
            await asyncio.sleep(min(interval, remaining))
            interval = min(interval * POLL_BACKOFF_FACTOR, POLL_INTERVAL_MAX_SEC)
            try:
                current = await self._within(self._get_task(current.id), deadline)
            except asyncio.TimeoutError:
                if not cancel_on_timeout:
                    return self._format_state(current)
                return await self._timed_out(current, timeout_sec)
            except Exception as exc:
                log.warning("A2A poll of %s task %s failed: %s", self.entry.id, current.id, exc)
                return format_a2a_error(self.entry.id, f"lost track of task {current.id}: {exc}")

    async def _timed_out(self, task: Task, timeout_sec: float) -> str:
        """The task id survives the failure: whatever the agent produced stays retrievable."""
        try:
            await asyncio.wait_for(self._cancel_task(task.id), timeout=CANCEL_ON_TIMEOUT_BUDGET_SEC)
            cancelled = "cancel requested"
        except Exception as exc:
            log.warning("A2A cancel of %s task %s failed: %s", self.entry.id, task.id, exc)
            cancelled = f"cancel request failed: {exc}"
        return format_a2a_timeout(self.entry.id, f"no result within {timeout_sec:g}s ({cancelled}); task_id={task.id}")

    def _format_submitted(self, task: Task) -> str:
        return (
            f"A2A task submitted to {self.entry.id}: task_id={task.id} "
            f"state={task_state_name(task.status.state)}.\n"
            f"The remote agent is working. Collect the result with "
            f'a2a_task_status(agent_id="{self.entry.id}", task_id="{task.id}").'
        )

    def _format_state(self, task: Task) -> str:
        if is_terminal_task_state(task.status.state):
            return self._format_terminal(task)
        line = f"A2A task {task.id} on {self.entry.id} is {task_state_name(task.status.state)}."
        note = _status_text(task)
        return f"{line}\n{note}" if note else line

    def _format_terminal(self, task: Task) -> str:
        state = task_state_name(task.status.state)
        if state == "completed":
            body = _artifact_text(task) or _status_text(task)
            return body or f"A2A task {task.id} on {self.entry.id} completed with no output."
        detail = _status_text(task) or _artifact_text(task)
        message = f"remote task {task.id} ended in state '{state}'"
        return format_a2a_error(self.entry.id, f"{message}: {detail}" if detail else message)

    def _format_interrupted(self, task: Task, state: str) -> str:
        if state == "auth-required":
            # The configured credential is wrong: an operator's problem, not one answering can
            # fix. Left alive so a fixed token can still reach it.
            detail = _status_text(task)
            return format_a2a_error(
                self.entry.id,
                "the remote agent rejected our credentials (auth-required) — check this agent's "
                f"auth_header/auth_token in settings; task_id={task.id}" + (f": {detail}" if detail else ""),
            )
        return format_a2a_input_required(self.entry.id, _status_text(task) or _artifact_text(task), task.id)


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
