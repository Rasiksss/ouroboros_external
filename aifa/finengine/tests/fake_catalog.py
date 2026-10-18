"""Подставной каталог файлов ouroboros для модульных тестов службы: `POST/GET/DELETE /api/files*`.

Настоящий HTTP на свободном порту (поток `http.server`): `CatalogFiles` проходит всю сеть — заголовки,
multipart, потоковое чтение. Формы ответов (`FileInfo`, коды отказов) — те, что отдаёт
`transport/browser/files.py`; контрактные тесты C6 (`aifa/tests/contract`) идут на настоящий ouroboros и
подтверждают их. Список форматов ниже — копия списка каталога (`security_config/attachments.py`) для двойника,
а не для службы: служба своего списка не держит (ADR-0026).
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

OWNER_HEADER = "x-ouroboros-owner-id"
ALLOWED_SUFFIXES = frozenset(
    {".pdf", ".xlsx", ".docx", ".pptx", ".txt", ".md", ".csv", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
)
MAX_BYTES = 50 * 1024 * 1024


@dataclass
class StoredBlob:
    owner: str
    name: str
    mime: str
    content: bytes


@dataclass
class FakeCatalog:
    """`files` — что лежит в каталоге; `requests` — `(метод, путь, владелец)` каждого обращения по порядку.

    `down` — каталог отвечает 503 на всё (лежит); `refuse_with` — следующий `POST /api/files` отвечает этим
    `(код HTTP, код отказа)`; `failing_reads` — столько следующих `GET` отвечают 503 (каталог перезапускается).
    """

    files: dict[str, StoredBlob] = field(default_factory=dict)
    gone: dict[str, str] = field(default_factory=dict)  # удалённые файлы -> владелец: повторное удаление — 204, как у ouroboros
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    down: bool = False
    refuse_with: tuple[int, str] | None = None
    failing_reads: int = 0
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    def __enter__(self) -> FakeCatalog:
        catalog = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # тишина в выводе pytest
                return

            def _owner(self) -> str:
                return self.headers.get(OWNER_HEADER, "")

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _gate(self) -> bool:
                catalog.requests.append((self.command, self.path, self._owner()))
                if catalog.down:
                    self._json(503, {"ok": False, "code": "file_storage_unavailable", "retryable": True})
                    return False
                if not self._owner():
                    self._json(403, {"error": f"{OWNER_HEADER} is absent or empty"})
                    return False
                return True

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if not self._gate():
                    return
                name, content = _file_part(self.headers.get("Content-Type", ""), body)
                if catalog.refuse_with is not None:
                    status, code = catalog.refuse_with
                    catalog.refuse_with = None
                    self._json(status, {"ok": False, "code": code, "message": "refused", "retryable": False})
                    return
                suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if suffix not in ALLOWED_SUFFIXES:
                    self._json(415, {"ok": False, "code": "attachment_type_not_allowed",
                                     "message": "unsupported file format", "filename": name, "retryable": False})
                    return
                if len(content) > MAX_BYTES:
                    self._json(413, {"ok": False, "code": "attachment_too_large", "retryable": False})
                    return
                file_id = uuid.uuid4().hex
                mime = _MIME.get(suffix, "application/octet-stream")
                catalog.files[file_id] = StoredBlob(self._owner(), name, mime, content)
                self._json(201, _info(file_id, catalog.files[file_id]))

            def _mine(self, file_id: str) -> StoredBlob | None:
                blob = catalog.files.get(file_id)
                return blob if blob is not None and blob.owner == self._owner() else None

            def do_GET(self) -> None:
                if not self._gate():
                    return
                if catalog.failing_reads > 0:
                    catalog.failing_reads -= 1
                    self._json(503, {"ok": False, "code": "file_storage_unavailable", "retryable": True})
                    return
                match = re.fullmatch(r"/api/files/([0-9a-f]{32})(/content)?", self.path.partition("?")[0])
                blob = self._mine(match.group(1)) if match else None
                if match is None or blob is None:
                    self._json(404, {"ok": False, "code": "file_not_found", "retryable": False})
                    return
                if not match.group(2):
                    self._json(200, _info(match.group(1), blob))
                    return
                self.send_response(200)
                self.send_header("Content-Type", blob.mime)
                self.send_header("Content-Length", str(len(blob.content)))
                self.end_headers()
                self.wfile.write(blob.content)

            def do_DELETE(self) -> None:
                if not self._gate():
                    return
                match = re.fullmatch(r"/api/files/([0-9a-f]{32})", self.path)
                if match is not None and catalog.gone.get(match.group(1)) == self._owner():
                    self.send_response(204)
                    self.end_headers()
                    return
                if match is None or self._mine(match.group(1)) is None:
                    self._json(404, {"ok": False, "code": "file_not_found", "retryable": False})
                    return
                catalog.gone[match.group(1)] = self._owner()
                del catalog.files[match.group(1)]
                self.send_response(204)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def asked(self, method: str) -> list[str]:
        return [path for verb, path, _owner in self.requests if verb == method]


_MIME = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _info(file_id: str, blob: StoredBlob) -> dict[str, Any]:
    return {
        "id": file_id, "name": blob.name, "folder_id": None, "mime": blob.mime, "size": len(blob.content),
        "status": "ready", "created_at": "2026-10-18T10:00:00+00:00", "updated_at": "2026-10-18T10:00:00+00:00",
        "expires_at": None, "url": f"/api/files/{file_id}/content",
    }


def _file_part(content_type: str, body: bytes) -> tuple[str, bytes]:
    """Имя и байты поля `file` из multipart/form-data."""
    boundary = re.search(r"boundary=([^;]+)", content_type)
    assert boundary, f"не multipart: {content_type!r}"
    delimiter = b"--" + boundary.group(1).strip('"').encode()
    for part in body.split(delimiter):
        head, separator, payload = part.partition(b"\r\n\r\n")
        found = re.search(rb'name="file"; filename="([^"]*)"', head)
        if separator and found:
            return found.group(1).decode("utf-8"), payload.removesuffix(b"\r\n")
    raise AssertionError("в запросе нет поля file")
