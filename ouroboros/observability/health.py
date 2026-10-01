from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response


async def health_probe(_: Request) -> Response:
    return Response(status_code=200)


async def readiness_probe(request: Request) -> Response:
    """Return 503 until supervisor (and bridge, when configured) are ready."""
    app = request.app
    ready_event = getattr(app.state, "supervisor_ready_event", None)
    get_error = getattr(app.state, "get_supervisor_error", None)

    if ready_event is not None and not ready_event.is_set():
        return Response(status_code=503, content="supervisor starting")

    if get_error is not None:
        error = get_error()
        if error:
            return Response(status_code=503, content=error[:500])

    try:
        from ouroboros.app.config import load_settings
        from ouroboros.app.server_runtime import has_supervisor_provider

        if has_supervisor_provider(load_settings()):
            from supervisor.runtime_context import try_get_bridge

            if try_get_bridge() is None:
                return Response(status_code=503, content="supervisor bridge not ready")
    except Exception:
        pass

    return Response(status_code=200)
