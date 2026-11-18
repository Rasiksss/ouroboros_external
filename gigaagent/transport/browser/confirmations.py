"""The owner's answer to a gated tool call as a click: this submits the message, the supervisor decides."""

from __future__ import annotations

import asyncio
import logging
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.contracts.chat_commands import is_confirmation_id
from ouroboros.contracts.tool_confirmation import CONFIRMATION_DECISIONS
from ouroboros.observability import aef_tracing
from transport.browser._helpers import json_error, state_attr
from transport.browser.contracts import ConfirmationDecisionResponse
from transport.browser.identity import IdentityRefused, browser_identity
from transport.interfaces import InboundEnvelope

log = logging.getLogger(__name__)


async def api_confirmation_decision(request: Request) -> JSONResponse:
    try:
        scope = await browser_identity.resolver.scope_for(request.headers)
    except IdentityRefused as refusal:
        return json_error(refusal.reason, status=refusal.status)

    try:
        payload = await request.json()
    except Exception:
        return json_error("invalid json", status=400, code="invalid_json")
    if not isinstance(payload, dict):
        return json_error("invalid payload", status=400, code="invalid_payload")

    confirmation_id = str(request.path_params.get("confirmation_id") or "").strip().lower()
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in CONFIRMATION_DECISIONS or not is_confirmation_id(confirmation_id):
        return json_error("invalid confirmation decision", status=400, code="invalid_decision")

    gateway = state_attr(request, "message_gateway")
    if gateway is None:
        return json_error("message gateway unavailable", status=503, code="gateway_unavailable", retryable=True)

    envelope = InboundEnvelope(
        scope=scope,
        user_id=scope.chat_id,
        confirmation_id=confirmation_id,
        confirmation_decision=decision,
        client_message_id=uuid.uuid4().hex,
        trace_id=aef_tracing.current_trace_id(),
        # A click is not a line in the conversation; the question and the reply to it are.
        suppress_chat_log=True,
    )
    try:
        await asyncio.to_thread(gateway.inbox.submit, envelope)
    except Exception:
        log.warning("Failed to submit a browser confirmation decision", exc_info=True)
        return json_error(
            "failed to submit the decision",
            status=503,
            code="decision_submit_failed",
            retryable=True,
        )
    submitted: ConfirmationDecisionResponse = {"ok": True, "status": "submitted"}
    return JSONResponse(submitted, status_code=202)


__all__ = ["api_confirmation_decision"]
