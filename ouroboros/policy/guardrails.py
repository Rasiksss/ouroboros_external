"""AEF Guardrails (GR Services) /check-only client, monitoring mode."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from new_core.llm.types import LLMMessage
from new_core.loop.interfaces import LLMMiddleware
from ouroboros.observability import aef_tracing

log = logging.getLogger(__name__)

Direction = Literal["input", "output"]
Decision = Literal["ALLOW", "BLOCK", "ERROR"]


@dataclass(frozen=True)
class GuardrailVerdict:
    direction: Direction
    decision: Decision
    reason: str = ""


class _CheckOnlyMessage(BaseModel):
    content: str


class _CheckOnlyChoice(BaseModel):
    message: _CheckOnlyMessage


class _CheckOnlyResponse(BaseModel):
    """The OpenAI-style body of /check-only; the first choice mirrors the header decision."""

    choices: list[_CheckOnlyChoice] = Field(min_length=1)


class GuardrailsClient:
    """One HTTP client to GR Services for the life of the process."""

    def __init__(
        self,
        *,
        url: str,
        agent_id: str,
        model: str,
        timeout: float,
        connect_timeout: float,
        verify: bool,
        client_factory: Callable[..., httpx.Client] | None = None,
    ) -> None:
        self._url = url
        self._agent_id = agent_id
        self._model = model
        factory = client_factory or httpx.Client
        self._http = factory(timeout=httpx.Timeout(timeout, connect=connect_timeout), verify=verify)

    def check(self, text: str, direction: Direction) -> GuardrailVerdict:
        """Ask GR Services for the aggregate ALLOW/BLOCK decision on one message."""
        # The client outlives every check, so the identifier is read here rather than merged in at
        # construction, which would label every later check with the operation that built it.
        # `with_trace_header` omits the header when no identifier is active, and sends the dashed
        # UUID spelling GR's examples use.
        headers = aef_tracing.with_trace_header({"DIRECTION": direction})
        if self._agent_id:
            # Absent rather than empty, the rule every other outbound integration follows: an
            # empty value passes a validator and identifies nobody. Set here rather than through
            # `with_agent_header` because GR reads the lowercase spelling and the two spellings
            # are two dict keys — merging both would put the same header on the wire twice.
            headers["x-agent-id"] = self._agent_id
        body = {
            "model": self._model,
            "messages": [
                {"role": "assistant" if direction == "output" else "user", "content": text},
            ],
            "stream": False,
        }
        try:
            response = self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return GuardrailVerdict(direction=direction, decision="ERROR", reason=str(exc))
        if not response.is_success:
            return GuardrailVerdict(
                direction=direction, decision="ERROR", reason=f"GR returned HTTP {response.status_code}"
            )
        decision = self._parse_decision(response.headers.get(f"gr-decision-{direction}"))
        if decision is None:
            decision = self._parse_decision(self._response_content(response))
        if decision is None:
            return GuardrailVerdict(direction=direction, decision="ERROR", reason="cannot parse GR decision")
        return GuardrailVerdict(direction=direction, decision=decision)

    @staticmethod
    def _parse_decision(value: str | None) -> Decision | None:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized == "ALLOW":
                return "ALLOW"
            if normalized == "BLOCK":
                return "BLOCK"
        return None

    @staticmethod
    def _response_content(response: httpx.Response) -> str | None:
        # The body is the fallback for proxies that strip headers.
        try:
            return _CheckOnlyResponse.model_validate(response.json()).choices[0].message.content
        except ValueError:
            # Covers both a non-JSON body and a body outside the agreed shape: ValidationError is a ValueError.
            return None


class GuardrailsMiddleware(LLMMiddleware):
    """Monitoring on the dialogue boundaries: a new owner message in, an answer without tool calls out.

    Tool rounds would send the assembled prompt, and the GR SLA is seconds per check.
    """

    def __init__(self, client: GuardrailsClient) -> None:
        self._client = client
        self._monitored: dict[Direction, str] = {"input": "", "output": ""}

    def before_call(self, messages: list[dict[str, Any]]) -> None:
        self._monitor(self._latest_owner_message(messages), "input")

    def after_call(self, response: LLMMessage | None) -> None:
        if response is not None and not response.tool_calls:
            self._monitor(response.content, "output")

    def _monitor(self, text: str | None, direction: Direction) -> None:
        """One check per new text on a boundary; degrades to a log line, never to a failed task."""
        checked = str(text or "")
        if not checked.strip() or checked == self._monitored[direction]:
            return
        self._monitored[direction] = checked
        try:
            verdict = self._client.check(checked, direction)
        except Exception as exc:
            # A malformed URL raises httpx.InvalidURL, which is not an httpx.HTTPError.
            log.warning("Guardrails %s check could not run: %r", direction, exc)
            return
        if verdict.decision == "BLOCK":
            log.warning("Guardrails %s check: BLOCK", direction)
        elif verdict.decision == "ERROR":
            log.warning("Guardrails %s check: ERROR %s", direction, verdict.reason)
        else:
            log.debug("Guardrails %s check: ALLOW", direction)

    @staticmethod
    def _latest_owner_message(messages: list[dict[str, Any]]) -> str:
        """The newest user text in the transcript; compaction rewrites the list, so it is re-read."""
        for message in reversed(messages):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str):
                return content
        return ""
