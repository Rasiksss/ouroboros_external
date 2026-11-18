"""Helpers for normalized session-relative filesystem paths."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.utils.exceptions.common import ValidationErrorException


def normalize_virtual_path(virtual_path: str) -> str:
    raw = (virtual_path or "").strip()
    if not raw:
        raw = "/"
    if not raw.startswith("/"):
        raw = f"/{raw}"

    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in {"", "/", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValidationErrorException(
                    "path traversal is not allowed",
                    details={"path": virtual_path},
                )
            parts.pop()
            continue
        parts.append(part)

    return "/" + "/".join(parts)


def map_virtual_to_host(rootfs_path: str, virtual_path: str) -> Path:
    normalized = normalize_virtual_path(virtual_path)
    if normalized == "/":
        return Path(rootfs_path)
    return Path(rootfs_path) / normalized.lstrip("/")
