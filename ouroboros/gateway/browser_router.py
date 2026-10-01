"""Optional browser route collection."""

from __future__ import annotations

import pathlib

from starlette.routing import BaseRoute, Route, WebSocketRoute

from ouroboros.gateway.browser_chat import (
    api_chat_file_download,
    api_chat_history,
    api_chat_upload,
    api_chat_upload_delete,
)
from ouroboros.gateway.browser_state import api_health, api_state
from ouroboros.gateway.browser_ws import ws_endpoint


def collect_routes(
    *,
    data_dir: pathlib.Path,
    repo_dir: pathlib.Path,
) -> list[BaseRoute]:
    del data_dir, repo_dir  # reserved for chat/state handlers

    return [
        Route("/api/health", endpoint=api_health, methods=["GET"]),
        Route("/api/state", endpoint=api_state, methods=["GET"]),
        Route("/api/chat/history", endpoint=api_chat_history, methods=["GET"]),
        Route("/api/chat/files/{path:path}", endpoint=api_chat_file_download, methods=["GET"]),
        Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
        Route("/api/chat/upload", endpoint=api_chat_upload_delete, methods=["DELETE"]),
        WebSocketRoute("/ws", endpoint=ws_endpoint),
    ]


__all__ = ["collect_routes"]
