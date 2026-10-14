"""File bridge for AEF WorkStation (spec §19.2a).

Not tools: the model-facing ``file_read``/``file_write``/``file_edit``/
``apply_patch`` handlers are the only callers, plus each other (``edit``
reads then writes; ``apply_patch`` reads/writes/removes/renames per file).
This is the single place that normalizes paths (spec §13.2) and enforces
the workspace access level (spec §8.4) — a tool handler never talks to the
client directly.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from typing import Any

from ouroboros.egress.workstation.client import (
    WorkstationAccessDenied,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
    WorkstationProtocolError,
)
from ouroboros.egress.workstation.executor import WorkstationCommandExecutor
from ouroboros.egress.workstation.paths import normalize_workspace_path, to_command_relative


@dataclass(frozen=True)
class FileStat:
    path: str
    kind: str
    bytes: int | None = None
    sha256: str | None = None


class WorkstationFileBridge:
    def __init__(
        self,
        client: WorkstationClient,
        executor: WorkstationCommandExecutor,
        *,
        session_id: str,
        token: str,
        config: WorkstationConfig,
    ) -> None:
        self._client = client
        self._executor = executor
        self._session_id = session_id
        self._token = token
        self._config = config

    def _normalize(self, path: str) -> str:
        return normalize_workspace_path(path, workspace_root=self._config.workspace_root)

    def _require_write_access(self) -> None:
        if self._config.access_level != "rw":
            raise WorkstationAccessDenied(
                f"workspace access level is {self._config.access_level!r}; writing is not permitted"
            )

    def read_file(self, path: str) -> bytes:
        virtual = self._normalize(path)
        return self._client.download_file(session_id=self._session_id, token=self._token, path=virtual)

    def write_file(self, path: str, data: bytes, *, mime: str = "application/octet-stream") -> None:
        self._require_write_access()
        virtual = self._normalize(path)
        self.mkdirp(posixpath.dirname(virtual))
        filename = posixpath.basename(virtual) or "file"
        self._client.upload_file(
            session_id=self._session_id, token=self._token, path=virtual, data=data, filename=filename, mime=mime
        )

    def stat(self, path: str) -> FileStat | None:
        virtual = self._normalize(path)
        try:
            node = self._client.list_files(
                session_id=self._session_id, token=self._token, path=virtual, recursive=False
            )
        except WorkstationApiError as exc:
            if exc.is_not_found:
                return None
            raise
        kind = str(node.get("kind") or "")
        if not kind:
            return None
        size = node.get("bytes")
        return FileStat(
            path=virtual,
            kind=kind,
            bytes=int(size) if isinstance(size, (int, float)) else None,
            sha256=node.get("sha256") if isinstance(node.get("sha256"), str) else None,
        )

    def readdir(self, path: str) -> dict[str, Any]:
        virtual = self._normalize(path)
        try:
            return self._client.list_files(session_id=self._session_id, token=self._token, path=virtual, recursive=True)
        except WorkstationApiError as exc:
            if exc.is_not_found:
                return {"path": virtual, "kind": "dir", "children": []}
            raise

    def mkdirp(self, path: str) -> None:
        self._require_write_access()
        virtual = self._normalize(path)
        if virtual == self._config.workspace_root:
            return
        relative = to_command_relative(virtual, workspace_root=self._config.workspace_root)
        self._run_shell(f"mkdir -p {shlex.quote(relative)}")

    def remove(self, path: str, *, recursive: bool = False) -> None:
        self._require_write_access()
        virtual = self._normalize(path)
        relative = to_command_relative(virtual, workspace_root=self._config.workspace_root)
        flag = "-rf" if recursive else "-f"
        self._run_shell(f"rm {flag} {shlex.quote(relative)}")

    def rename(self, src: str, dst: str) -> None:
        self._require_write_access()
        virtual_src = self._normalize(src)
        virtual_dst = self._normalize(dst)
        self.mkdirp(posixpath.dirname(virtual_dst))
        relative_src = to_command_relative(virtual_src, workspace_root=self._config.workspace_root)
        relative_dst = to_command_relative(virtual_dst, workspace_root=self._config.workspace_root)
        self._run_shell(f"mv {shlex.quote(relative_src)} {shlex.quote(relative_dst)}")

    def _run_shell(self, script: str) -> None:
        outcome = self._executor.run_buffered(["/bin/sh", "-c", script], workdir=self._config.workspace_root)
        if outcome.exit_code != 0:
            raise WorkstationProtocolError(
                f"WorkStation shell helper failed (exit {outcome.exit_code}): {outcome.stderr or outcome.stdout}"
            )


__all__ = ["FileStat", "WorkstationFileBridge"]
