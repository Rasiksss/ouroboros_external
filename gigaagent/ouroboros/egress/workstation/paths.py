"""Virtual path normalization for AEF WorkStation."""

from __future__ import annotations

import posixpath

#: Fixed subtree for turn-attachment uploads (spec §9); also the seeding
#: walk-exclusion — one constant, used in both places so the two can never
#: drift apart.
INBOUND_SUBDIR = "media/inbound"


class WorkstationPathError(ValueError):
    """A path could not be normalized to a safe, in-workspace virtual path."""


def normalize_workspace_path(path: str, *, workspace_root: str) -> str:
    """Normalize a model- or tool-supplied path to a virtual absolute path
    under ``workspace_root`` (spec §13.2).

    In order:
    1. Already starts with the canonical root -> use as-is.
    2. Not absolute -> resolve relative to the root (never by guessing a
       last-segment match against the root's own name — a legitimate
       subdirectory sharing that name must not be mistaken for the root).
    3. Absolute but not under the root -> find the *last* path segment equal
       to the root's own name and take everything after it as relative (the
       shape a path takes when it was read back from an un-chrooted shell,
       spec §8.3).
    4. Reject anything that still resolves outside the root (no `..` escape).

    This integration does not bridge any other internal storage path
    convention into WorkStation: every path a model or tool passes here is
    already virtual, relative, or a stray absolute host-looking path per
    step 3.
    """
    root = _clean_root(workspace_root)
    raw = str(path or "").strip()
    if not raw:
        raise WorkstationPathError("empty path")
    normalized_raw = raw.replace("\\", "/")

    if normalized_raw == root or normalized_raw.startswith(root + "/"):
        candidate = normalized_raw
    elif not normalized_raw.startswith("/"):
        candidate = posixpath.join(root, normalized_raw)
    else:
        root_name = root.rsplit("/", 1)[-1]
        parts = [part for part in normalized_raw.split("/") if part]
        last_index = _last_index(parts, root_name)
        if last_index is None:
            raise WorkstationPathError(f"path {path!r} is not under the workspace root {root!r}")
        candidate = posixpath.join(root, *parts[last_index + 1 :])

    collapsed = posixpath.normpath(candidate)
    if collapsed != root and not collapsed.startswith(root + "/"):
        raise WorkstationPathError(f"path {path!r} escapes the workspace root {root!r}")
    return collapsed


def to_command_relative(path: str, *, workspace_root: str) -> str:
    """Rewrite a normalized virtual path relative to ``workspace_root`` for use
    inside shell command *text* (spec §8.3).

    WorkStation does not chroot the command process: it sees the real
    filesystem, not a re-rooted view. ``cwd`` in the exec request is resolved
    server-side and a virtual absolute path there is fine — but a path
    written inside the command's own text/arguments is resolved by the
    already-running process after it has `cd`ed into that same `cwd`, so it
    must be relative to it, not an absolute virtual path.
    """
    normalized = normalize_workspace_path(path, workspace_root=workspace_root)
    root = _clean_root(workspace_root)
    if normalized == root:
        return "."
    return normalized.removeprefix(root).lstrip("/")


def _clean_root(workspace_root: str) -> str:
    root = posixpath.normpath(str(workspace_root or "/workspace"))
    return root if root.startswith("/") else f"/{root}"


def _last_index(parts: list[str], name: str) -> int | None:
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == name:
            return index
    return None


__all__ = [
    "INBOUND_SUBDIR",
    "WorkstationPathError",
    "normalize_workspace_path",
    "to_command_relative",
]
