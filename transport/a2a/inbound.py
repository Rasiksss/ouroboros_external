"""Reject oversized A2A messages before either JSON-RPC protocol adapter."""

from a2a.server.jsonrpc_models import InvalidParamsError
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from starlette.requests import Request
from starlette.responses import Response

from ouroboros.observability import aef_tracing
from transport.inbound_limits import oversized_message_refusal


class InboundJsonRpcDispatcher(JsonRpcDispatcher):
    def __init__(
        self, request_handler: RequestHandler, *, max_chars: int, context_builder: ServerCallContextBuilder
    ) -> None:
        super().__init__(request_handler, context_builder=context_builder, enable_v0_3_compat=True)
        self._max_chars = max_chars

    def _message_text(self, body: dict[str, object]) -> str:
        params = body.get("params")
        message = params.get("message") if isinstance(params, dict) else None
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            return ""
        legacy = body.get("method") in ("message/send", "message/stream")
        return "\n".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and (not legacy or part.get("kind", "text") == "text")
        )

    async def handle_requests(self, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return await super().handle_requests(request)
        if (
            isinstance(body, dict)
            and body.get("jsonrpc") == "2.0"
            and body.get("method") in ("message/send", "message/stream", "SendMessage", "SendStreamingMessage")
            and (body.get("id") is None or isinstance(body["id"], str | int))
        ):
            refusal = oversized_message_refusal(self._message_text(body), self._max_chars)
            if refusal:
                trace_id = aef_tracing.accept_trace_id_from_headers(request.headers, source="a2a")
                with aef_tracing.trace_scope(trace_id):
                    response = self._generate_error_response(body.get("id"), InvalidParamsError(message=refusal))
                    response.headers[aef_tracing.TRACE_HEADER_NAME] = trace_id
                    return response
        return await super().handle_requests(request)
