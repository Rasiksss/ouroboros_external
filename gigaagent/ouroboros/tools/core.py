"""File and data tools."""

from __future__ import annotations

import ast
import base64
import bisect
import json
import logging
import pathlib
import posixpath
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.storage.attachment_validation import attachment_mime_for_suffix
from ouroboros.storage.blob.drive import DriveStorage, get_drive_storage
from ouroboros.storage.blob.keys import parse_data_rel_path, retired_scope_for_rel, scope_crosses_owners
from ouroboros.storage.blob.scopes import ALL_SCOPES, SCOPE_MEMORY
from ouroboros.storage.chat_attachments import read_attachment_bytes, store_attachment_fileobj
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.service.artifacts import PROVENANCE_SUFFIX, provenance_path, resolve_artifact, write_provenance
from ouroboros.tools.service.messaging import _send_image, send_image_data
from ouroboros.utils import safe_relpath

log = logging.getLogger(__name__)

_MEMORY_AT_DRIVE_MEMORY = frozenset(
    {
        "scratchpad.md",
        "deep_review.md",
    }
)

_HIDDEN_DATA_PREFIXES = ("state/agent_notes/", "state/tmp/")


def _is_hidden_data_listing_item(item: str) -> bool:
    norm = str(item or "").replace("\\", "/").lstrip("./")
    return norm.endswith(PROVENANCE_SUFFIX) or any(norm.startswith(prefix) for prefix in _HIDDEN_DATA_PREFIXES)


def _drive_for(ctx: ToolContext) -> DriveStorage:
    """The drive this call was handed, not the one the process is configured with."""

    return get_drive_storage(pathlib.Path(ctx.drive_root))


def _read_drive_text(drive: DriveStorage, owner_scope: OwnerScope, path: pathlib.Path) -> str:
    """Text from the drive when the path names a storage scope, else from local disk."""
    rel = drive.rel_from_path(path)
    if rel is not None:
        try:
            parse_data_rel_path(rel)
        except ValueError:
            rel = None
    if rel is None:
        return path.read_text(encoding="utf-8")
    stored = drive.read_text(owner_scope, path)
    if stored is None:
        raise FileNotFoundError(path)
    return stored


def _read_data_provenance(drive: DriveStorage, owner_scope: OwnerScope, path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = drive.read_text(owner_scope, provenance_path(path))
        data = json.loads(raw) if raw is not None else None
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class ReadPage:
    header: str
    body: str
    footer: str
    start: int
    total: int

    def render(self) -> str:
        return self.header + self.body + self.footer


def read_page(
    path: str,
    content: str,
    max_lines: int = 2000,
    start_line: int = 1,
    *,
    start_char: int | None = None,
    max_chars: int | None = None,
) -> ReadPage:
    """Calculate the exact displayed range and its continuation."""
    start_raw, max_raw = coerce_line_window(start_line, max_lines)
    max_raw = max(1, max_raw)
    lines = content.splitlines(keepends=True)
    total = len(lines)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    if start_char is None:
        start = max(1, min(start_raw, total + 1))
        start_index = start - 1
        absolute_start = offsets[min(start_index, total)]
    else:
        try:
            char_raw = int(start_char)
        except (TypeError, ValueError):
            char_raw = 1
        absolute_start = max(0, min(char_raw - 1, len(content)))
        start_index = max(0, bisect.bisect_right(offsets, absolute_start) - 1)
        start = min(start_index + 1, total + 1)

    end_index = min(start_index + max_raw, total)
    absolute_end = offsets[end_index]
    body = content[absolute_start:absolute_end]
    line_header = f"# {path} — lines {start}\u2013{end_index} of {total}\n"
    line_footer = ""
    if max_chars is not None and end_index < total:
        line_footer = f"\n# More content available; continue with start_line={end_index + 1}, max_lines={max_raw}.\n"
    line_result = line_header + body + line_footer

    if start_char is None and (max_chars is None or len(line_result) <= max_chars):
        return ReadPage(line_header, body, line_footer, absolute_start, len(content))

    total_chars = len(content)
    complete_header = (
        f"# {path} — characters {absolute_start + 1}\u2013{absolute_end} "
        f"of {total_chars}; starts at line {start} of {total}\n"
    )
    complete_result = complete_header + body + line_footer
    if max_chars is None or len(complete_result) <= max_chars:
        return ReadPage(complete_header, body, line_footer, absolute_start, len(content))

    char_limit = max(1, int(max_chars))
    max_header = (
        f"# {path} — characters {absolute_start + 1}\u2013{total_chars} "
        f"of {total_chars}; starts at line {start} of {total}\n"
    )
    max_footer = f"\n# More content available; continue with start_char={total_chars}, max_lines={max_raw}.\n"
    body_limit = max(0, char_limit - len(max_header) - len(max_footer))
    page_end = min(absolute_end, absolute_start + body_limit)
    next_line_index = max(0, bisect.bisect_right(offsets, page_end) - 1)
    remaining_lines = max(1, end_index - next_line_index)
    header = (
        f"# {path} — characters {absolute_start + 1}\u2013{page_end} "
        f"of {total_chars}; starts at line {start} of {total}\n"
    )
    footer = f"\n# More content available; continue with start_char={page_end + 1}, max_lines={remaining_lines}.\n"
    return ReadPage(header, content[absolute_start:page_end], footer, absolute_start, len(content))


def render_line_slice(
    path: str,
    content: str,
    max_lines: int = 2000,
    start_line: int = 1,
    *,
    start_char: int | None = None,
    max_chars: int | None = None,
) -> str:
    return read_page(path, content, max_lines, start_line, start_char=start_char, max_chars=max_chars).render()


def coerce_line_window(start_line: Any = 1, max_lines: Any = 2000) -> tuple[int, int]:
    try:
        start_raw = int(start_line)
    except (TypeError, ValueError):
        start_raw = 1
    try:
        max_raw = int(max_lines)
    except (TypeError, ValueError):
        max_raw = 2000
    return start_raw, max(1, max_raw)


#: Directories whose capability moved into the runtime DB, mapped to the tools
#: that own them: (read tool, list tool). One rule for every migrated
#: capability instead of a shim per capability — so the generic tools hold no
#: per-capability knowledge that the next migration could forget to update.
@dataclass(frozen=True)
class _MigratedOwner:
    """What answers a refused path, said in the words each refusal needs.

    ``read_instead``, ``list_instead``, and ``write_instead`` are clauses, not
    tool names: the scratchpad has no read tool at all, and a refusal that named
    its *write* tool as the way to read would be an instruction to mutate
    working memory. ``write_instead`` is separate for the mirror-image reason —
    the tool that writes a memory is not the one that writes the scratchpad.
    """

    owner_tool: str
    read_instead: str
    list_instead: str
    write_instead: str


_MEMORY_OWNER = _MigratedOwner(
    owner_tool="memory_read",
    read_instead="read the entry by name with `memory_read`",
    list_instead="list the entries with `memory_list`",
    write_instead="save the entry by name with `memory_save`, or remove it with `memory_delete`",
)

#: The scratchpad is already in the prompt in full and has no read tool.
_SCRATCHPAD_OWNER = _MigratedOwner(
    owner_tool="update_scratchpad",
    read_instead="it is already in context as `## Scratchpad` — do not re-read it; `update_scratchpad` appends to it",
    list_instead="it is already in context as `## Scratchpad`; `update_scratchpad` is the only tool that touches it",
    write_instead="append a block with `update_scratchpad`; the buffer is append-only and has no bulk-rewrite path",
)

#: Retired outright, with no successor to name. Serving the leftover would hand
#: back a document the system stopped maintaining.
_RETIRED_OWNER = _MigratedOwner(
    owner_tool="memory_read",
    read_instead="this artifact is retired; durable memory is reached with `memory_read`",
    list_instead="this artifact is retired; durable memory is listed with `memory_list`",
    write_instead="this artifact is retired; durable memory is written with `memory_save`",
)

_MIGRATED_MEMORY_DIRS: tuple[tuple[str, _MigratedOwner], ...] = (
    ("memory/global", _MEMORY_OWNER),
    ("memory/knowledge", _MEMORY_OWNER),
)

_MIGRATED_MEMORY_FILES: dict[str, _MigratedOwner] = {
    "memory/knowledge_journal.jsonl": _MEMORY_OWNER,
    "memory/scratchpad.md": _SCRATCHPAD_OWNER,
    "scratchpad.md": _SCRATCHPAD_OWNER,
    "memory/scratchpad_blocks.json": _SCRATCHPAD_OWNER,
    "memory/scratchpad_journal.jsonl": _SCRATCHPAD_OWNER,
    "memory/world.md": _RETIRED_OWNER,
    "world.md": _RETIRED_OWNER,
}


def _is_cognitive_data_path(norm: str) -> bool:
    text = str(norm or "").replace("\\", "/").lstrip("./")
    return text.startswith("memory/") or text in _MEMORY_AT_DRIVE_MEMORY


def _refusal_key(norm: str) -> str:
    """Normalize a path the way the resolver downstream will, then casefold it.

    The table is compared against a string while the caller resolves the same
    string through ``pathlib``, which collapses ``.``, ``..`` and doubled
    separators — so ``memory/./global/x`` used to walk past a refusal and reach
    the legacy blob. Case is folded because the drive may sit on a
    case-insensitive filesystem, where ``Memory/global/x`` opens the same file.
    """
    text = str(norm or "").replace("\\", "/").strip()
    if not text:
        return ""
    collapsed = posixpath.normpath(text)
    return collapsed.lstrip("/").rstrip("/").lower()


def _retired_scope_refusal(marker: str, scope: str) -> str:
    return (
        f"⚠️ {marker}: the {scope} scope was removed from storage, so a path under it addresses "
        "no durable object, and the disk of the instance that answered is not consulted. "
        "Use uploads/<filename> for a deliverable."
    )


def _retired_scope_for_target(drive: DriveStorage, target: pathlib.Path) -> str:
    rel = drive.rel_from_path(target)
    return retired_scope_for_rel(rel) if rel is not None else ""


def _refused_write_target(drive: DriveStorage, target: pathlib.Path) -> str:
    retired = _retired_scope_for_target(drive, target)
    if retired:
        return _retired_scope_refusal("DATA_WRITE_BLOCKED", retired)
    rel = drive.rel_from_path(target)
    if rel is None:
        return ""
    try:
        scope, _key = parse_data_rel_path(rel)
    except ValueError:
        return ""
    if scope != SCOPE_MEMORY:
        return ""
    return (
        f"⚠️ DATA_WRITE_BLOCKED: {rel} is a managed memory path; it is updated via "
        "memory_save/memory_delete, not generic data_write."
    )


def _unkeyed_scope_route(drive: DriveStorage, target: pathlib.Path) -> str:
    """Name the owner-less scope ``target`` resolves into, or ``""`` for none.

    ``scope_crosses_owners`` is the storage layer's answer for whether two owners writing
    one relative path land on one physical key, so the second write overwrites the first
    and the first owner's next read is served the second's bytes. The scopes it names hold
    instance state their own services write under a service identity — a generic,
    per-caller write is the one thing that turns a shared key into somebody's content.

    Asked of the *resolved* path because a symlink inside the data directory separates the
    name the caller wrote from the scope the bytes land in.
    """
    rel = drive.rel_from_path(target)
    if rel is None:
        return ""
    try:
        scope, _key = parse_data_rel_path(rel)
    except ValueError:
        return ""
    if not scope_crosses_owners(scope):
        return ""
    return f"the {scope} scope"


def _migrated_memory_owner(norm: str) -> _MigratedOwner | None:
    """The owner of a migrated path, else ``None``.

    Generic data tools refuse these paths rather than serving them. The rows
    belong to the owning tool, and the blobs the cutover left at the same paths
    are a rollback net whose contents are stale by construction.
    """
    text = _refusal_key(norm)
    owner = _MIGRATED_MEMORY_FILES.get(text)
    if owner is not None:
        return owner
    for prefix, dir_owner in _MIGRATED_MEMORY_DIRS:
        if text == prefix or text.startswith(f"{prefix}/"):
            return dir_owner
    return None


def _looks_like_serialized_tool_result(content: Any) -> bool:
    text = str(content or "").lstrip()
    if not (text.startswith(("{'content'", '{"content"'))):
        return False
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        try:
            parsed = json.loads(text)
        except Exception:
            return False
    return isinstance(parsed, dict) and isinstance(parsed.get("content"), str)


def _list_dir(
    drive: DriveStorage,
    owner_scope: OwnerScope,
    root: pathlib.Path,
    rel: str,
    max_entries: int = 500,
) -> list[str]:
    norm_rel = safe_relpath(rel)
    try:
        # relative_to raises for a root outside the drive; the local branch below answers it.
        root_resolved = pathlib.Path(root).resolve()
        if root_resolved == drive.drive_root:
            list_rel = "" if norm_rel in (".", "") else norm_rel
        else:
            base = root_resolved.relative_to(drive.drive_root).as_posix()
            list_rel = base if norm_rel in (".", "") else f"{base}/{norm_rel}".strip("/")
        # A path inside a storage scope is answered by the drive alone. The filesystem
        # branch below would list the scope's physical owner segment directories, which is
        # every other owner's namespace.
        scoped = list_rel.split("/", 1)[0].lower() in ALL_SCOPES if list_rel else False
        try:
            raw_entries = drive.list_entries(owner_scope, list_rel)
            # ``list_entries`` answers child names; the hidden-prefix rule is written
            # against the drive-relative path, so rejoin before filtering.
            entries = [
                entry
                for entry in raw_entries
                if not _is_hidden_data_listing_item(f"{list_rel}/{entry}" if list_rel else entry)
            ]
            if entries:
                items = entries[:max_entries]
                if len(entries) > max_entries:
                    items.append(f"...(truncated at {max_entries})")
                return items
            if raw_entries or scoped:
                return [] if raw_entries else [f"⚠️ Directory not found: {rel}"]
            if list_rel:
                return [f"⚠️ Directory not found: {rel}"]
        except ValueError:
            if scoped:
                return [f"⚠️ Directory not found: {rel}"]
    except Exception:
        log.debug("_list_dir drive listing failed for %s", rel, exc_info=True)

    target = (root / norm_rel).resolve()
    if not target.exists():
        return [f"⚠️ Directory not found: {rel}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {rel}"]
    local_items: list[str] = []
    try:
        for entry in sorted(target.iterdir()):
            item = str(entry.relative_to(root)) + ("/" if entry.is_dir() else "")
            if _is_hidden_data_listing_item(item):
                continue
            if len(local_items) >= max_entries:
                local_items.append(f"...(truncated at {max_entries})")
                break
            local_items.append(item)
    except Exception as e:
        local_items.append(f"⚠️ Error listing: {e}")
    return local_items


def _normalize_data_read_path(ctx: ToolContext, path: str) -> str:
    """Normalize paths that redundantly include the drive root."""

    norm = str(path).strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    drive_str = str(ctx.drive_root).rstrip("/")
    drive_no_lead = drive_str.lstrip("/")
    if drive_no_lead and norm.lstrip("/").startswith(drive_no_lead):
        stripped = norm.lstrip("/")
        norm = stripped[len(drive_no_lead) :].lstrip("/")
    elif norm.startswith(".tmp-data-") or norm.lstrip("/").startswith(".tmp-data-"):
        candidate = norm.lstrip("/")
        first_slash = candidate.find("/")
        if first_slash > 0:
            after = candidate[first_slash + 1 :]
            if after.startswith("data/"):
                norm = after[len("data/") :]
            else:
                norm = after
    root = pathlib.Path(ctx.drive_root).resolve()
    canonical = (root / norm).resolve().relative_to(root).as_posix()
    scope, separator, key = canonical.partition("/")
    return scope.lower() + separator + key if scope.lower() in ALL_SCOPES else canonical


def _data_read(ctx: ToolContext, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Read a drive text file; duplicate drive_root prefixes are stripped."""
    drive = _drive_for(ctx)
    norm = _normalize_data_read_path(ctx, path)
    # Refused before any target is resolved, so no branch downstream can answer
    # a confined task's path from a process-wide memory service.
    migrated = _migrated_memory_owner(norm)
    if migrated is not None:
        return f"⚠️ DATA_READ_BLOCKED: not readable through generic data_read — {migrated.read_instead}."
    retired = retired_scope_for_rel(_refusal_key(norm))
    if retired:
        return _retired_scope_refusal("DATA_READ_BLOCKED", retired)
    target = ctx.drive_path(norm)
    retired_target = _retired_scope_for_target(drive, target)
    if retired_target:
        return _retired_scope_refusal("DATA_READ_BLOCKED", retired_target)
    try:
        if norm.startswith("uploads/"):
            file = resolve_artifact(ctx, norm)
            if file is None:
                raise FileNotFoundError(norm)
            data = read_attachment_bytes(ctx.scope, drive.provider, file["storage_key"], mime=file["mime"])
            if data is None:
                raise FileNotFoundError(norm)
            content = data.decode("utf-8")
            provenance = {"origin": file["source"]}
        else:
            content = _read_drive_text(drive, ctx.scope, target)
            provenance = _read_data_provenance(drive, ctx.scope, target)
        warning = ""
        if provenance.get("origin") == "agent_generated":
            warning = (
                "DATA_PROVENANCE_WARNING: agent-generated artifact, not authoritative memory. "
                "Do not treat this file as a durable user/project fact unless confirmed by an authoritative source.\n\n"
            )
        start_raw, max_raw = coerce_line_window(start_line, max_lines)
        if _is_cognitive_data_path(norm) and start_raw == 1 and max_raw == 2000:
            return warning + content
        return warning + render_line_slice(norm, content, max_lines=max_raw, start_line=start_raw)
    except FileNotFoundError:
        if norm.replace("\\", "/").startswith("memory/"):
            explanation = (
                "Files that still live under memory/ are written lazily by "
                "the services that own them. Treat this as an empty/absent "
                "state and proceed with initialization if that is the task. "
                "Memory that moved to the runtime database never reaches "
                "this branch — it is refused, not reported missing."
            )
        else:
            explanation = (
                "This path does not exist yet. Treat it as an empty/absent "
                "state. Lazy-creation is not guaranteed for paths outside "
                "memory/; if this path was expected to exist, verify it was "
                "written correctly."
            )
        return f"⚠️ DATA_NOT_YET_CREATED: {path}\n\n{explanation} Use data_list to confirm what currently exists."


def _data_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    drive = _drive_for(ctx)
    dir = _normalize_data_read_path(ctx, dir)
    # Refused ahead of the confinement branch, matching _data_read: a listing
    # that disagreed with the owning tool would be worse than no listing.
    migrated = _migrated_memory_owner(dir)
    if migrated is not None:
        return json.dumps(
            [f"⚠️ DATA_LIST_BLOCKED: not listable through generic data_list — {migrated.list_instead}."],
            ensure_ascii=False,
            indent=2,
        )
    retired = retired_scope_for_rel(_refusal_key(dir)) or _retired_scope_for_target(
        drive, (pathlib.Path(ctx.drive_root) / safe_relpath(dir)).resolve()
    )
    if retired:
        return json.dumps(
            [_retired_scope_refusal("DATA_LIST_BLOCKED", retired)],
            ensure_ascii=False,
            indent=2,
        )
    if dir.rstrip("/") == "uploads" or dir.startswith("uploads/"):
        prefix = dir.removeprefix("uploads").strip("/")
        if prefix:
            prefix += "/"
        with resolve_runtime_engine("tool file list").connect() as conn:
            files = FileCatalog(conn, ctx.scope).tool_files(
                explicit_ids=[str(item["id"]) for item in ctx.attachments or [] if "id" in item],
                prefix=prefix,
                limit=max_entries,
            )
        names = [path.removeprefix(prefix) for path in files]
        entries = sorted({name.split("/", 1)[0] + ("/" if "/" in name else "") for name in names})
        return json.dumps(entries, ensure_ascii=False)
    # A parent such as memory/ still enumerates: the unmigrated files there are
    # readable, and descending into a migrated subdirectory — or naming a
    # migrated file — is refused above rather than filtered out here.
    return json.dumps(_list_dir(drive, ctx.scope, ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2)


def _data_write(
    ctx: ToolContext,
    path: str,
    content: str,
    mode: str = "overwrite",
) -> str:
    drive = _drive_for(ctx)
    write_path = _normalize_data_read_path(ctx, path)
    # Provenance sidecars are runtime-managed: a tool-authored one would let the
    # agent mark its own artifact authoritative and context-eligible.
    if write_path.lower().endswith(PROVENANCE_SUFFIX):
        return "⚠️ DATA_WRITE_BLOCKED: artifact provenance is runtime-managed."
    # Refused ahead of every branch below, matching _data_read and _data_list.
    # The same table both of them consult, so a path they refuse to serve is not
    # one this tool creates: writing `scratchpad.md` used to answer OK, write a
    # blob, and leave the agent with a file its own data_read then blocked —
    # bytes on disk that nothing in the runtime would ever read again.
    migrated = _migrated_memory_owner(write_path)
    if migrated is not None:
        return f"⚠️ DATA_WRITE_BLOCKED: not writable through generic data_write — {migrated.write_instead}."
    # Everything else under memory/ stays write-blocked as before. These are
    # owner mailboxes and dialogue blocks their own services write lazily; the
    # rule is broader than the migrated set on purpose, and narrowing it to the
    # table above would hand generic writes a tree that never accepted them.
    write_key = _refusal_key(write_path)
    if write_key == "memory" or write_key.startswith("memory/"):
        return (
            f"⚠️ DATA_WRITE_BLOCKED: {write_key} is a managed memory path; it must be updated via "
            "memory_save/memory_delete, not generic data_write."
        )
    # This half answers the name the caller wrote, including a bare `logs` that resolves to
    # no key; the resolved path is asked again below.
    retired = retired_scope_for_rel(write_key)
    if retired:
        return _retired_scope_refusal("DATA_WRITE_BLOCKED", retired)
    p = ctx.drive_path(write_path)
    # Asked again of the resolved target: a symlink (`uploads/sneak -> <DATA_DIR>/logs`)
    # the soft sandbox does not filter passes the check above under a name that is not a scope.
    resolved_refusal = _refused_write_target(drive, p)
    if resolved_refusal:
        return resolved_refusal
    unkeyed_route = _unkeyed_scope_route(drive, p)
    if unkeyed_route:
        return (
            f"⚠️ DATA_WRITE_BLOCKED: {unkeyed_route} carries no owner in its object keys, so a file "
            "written there is one every owner shares — the next write of the same name overwrites it "
            "and serves the wrong person's bytes back. Write a deliverable to uploads/<filename> instead."
        )
    # Defense-in-depth: settings.json is owner-only. Use inode-aware matching
    # for symlinks/hardlinks/case-insensitive APFS/NTFS, with a fallback for
    # not-yet-existing case variants.
    target_path = pathlib.Path(p)
    settings_path = load_path_settings().settings_path
    suffix = pathlib.PurePosixPath(str(path or "")).suffix.lower()
    if suffix in {".py", ".md", ".json", ".sh"} and _looks_like_serialized_tool_result(content):
        return (
            "⚠️ DATA_WRITE_BLOCKED: content looks like a serialized tool result "
            "object (for example {'content': ...}) rather than file text. "
            "Extract the actual file body before calling data_write."
        )
    matches = False
    try:
        if target_path.exists() and settings_path.exists():
            matches = target_path.samefile(settings_path)
    except OSError:
        matches = False
    if not matches:
        try:
            same_parent = target_path.parent.resolve() == settings_path.parent.resolve()
        except OSError:
            same_parent = False
        if same_parent and target_path.name.lower() == settings_path.name.lower():
            matches = True
    if matches:
        return (
            "⚠️ DATA_WRITE_BLOCKED: settings.json is not writable by tools in "
            "headless runtime. Change configuration via environment variables "
            "or the deployment config surface, then restart the server."
        )
    if write_path.startswith("uploads/"):
        if not ctx.task_id or write_path.startswith(("uploads/chat/", "uploads/_meta/")):
            return "⚠️ DATA_WRITE_BLOCKED: use a task-owned artifact name."
        existing_file = resolve_artifact(ctx, write_path)
        data = content.encode("utf-8")
        if mode == "append" and existing_file is not None:
            previous = read_attachment_bytes(
                ctx.scope, drive.provider, existing_file["storage_key"], mime=existing_file["mime"]
            )
            if previous is None:
                return "⚠️ DATA_WRITE_BLOCKED: existing artifact is unavailable; append needs its contents."
            data = previous + data
        with BytesIO(data) as fileobj:
            store_attachment_fileobj(
                ctx.scope,
                drive.provider,
                attachment_id=uuid.uuid4().hex,
                filename=p.name,
                mime=attachment_mime_for_suffix(p.name) or "application/octet-stream",
                fileobj=fileobj,
                size=fileobj.getbuffer().nbytes,
                status="ready",
                source="agent_generated",
                producer_task=str(ctx.task_id),
                artifact_path=write_path.removeprefix("uploads/"),
            )
        if existing_file is None:
            log.info("event=user_artifact_created task_id=%s scope=uploads size=%s", ctx.task_id, len(data))
        return f"OK: wrote {mode} {path} ({len(content)} chars)"
    if mode == "overwrite":
        drive.write_text(ctx.scope, p, content)
    else:
        rel = drive.rel_from_path(p)
        if rel is not None:
            existing = ""
            if drive.path_exists(ctx.scope, p):
                existing = drive.read_text(ctx.scope, p) or ""
            drive.write_text(ctx.scope, p, existing + content)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
    try:
        write_provenance(ctx, drive, p, write_path, created_by_tool="data_write")
    except Exception:
        log.debug("Failed to write data provenance for %s", write_path, exc_info=True)
    result = f"OK: wrote {mode} {path} ({len(content)} chars)"
    return result


def _send_photo(ctx: ToolContext, file_path: str = "", image_base64: str = "", caption: str = "") -> str:
    """Compatibility alias for the canonical image delivery path."""
    if file_path:
        return _send_image(ctx, file_path=file_path, caption=caption)
    if not image_base64:
        return "⚠️ Provide either file_path or image_base64."
    actual_b64 = image_base64
    if image_base64 == "__last_screenshot__":
        actual_b64 = str(getattr(ctx.browser_state, "last_screenshot_b64", "") or "")
        if not actual_b64:
            return "⚠️ No screenshot stored in browser state."
    limit = load_settings().OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES
    if len(actual_b64) > ((limit + 2) // 3) * 4:
        return f"⚠️ Image exceeds {limit} bytes limit."
    try:
        data = base64.b64decode(actual_b64, validate=True)
    except ValueError:
        return "⚠️ Image base64 is invalid."

    return send_image_data(ctx, data, caption=caption)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="data_read",
            schema=ToolSchema(
                name="data_read",
                description=(
                    "Read a UTF-8 text file from the local data directory. "
                    "Use max_lines (default 2000) and start_line (default 1) "
                    "to read large data/skill files in chunks."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_lines": {
                            "type": "integer",
                            "default": 2000,
                            "description": "Maximum number of lines to return (default 2000).",
                        },
                        "start_line": {
                            "type": "integer",
                            "default": 1,
                            "description": "1-indexed line to start reading from (default 1).",
                        },
                    },
                    "required": ["path"],
                },
            ),
            handler=_data_read,
        ),
        ToolEntry(
            name="data_list",
            schema=ToolSchema(
                name="data_list",
                description="List files under a local data directory.",
                parameters={
                    "type": "object",
                    "properties": {
                        "dir": {"type": "string", "default": "."},
                        "max_entries": {"type": "integer", "default": 500},
                    },
                    "required": [],
                },
            ),
            handler=_data_list,
        ),
        ToolEntry(
            name="data_write",
            schema=ToolSchema(
                name="data_write",
                description=(
                    "Write a UTF-8 text file to runtime data storage. "
                    "Use uploads/<filename> for a user deliverable sent with send_file; "
                    "other permitted owned paths remain valid. "
                    "Use mode='append' to write a large file in chunks across multiple calls "
                    "(useful when the full content exceeds a single LLM output budget)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to runtime DATA_DIR; use uploads/<filename> for "
                                "deliverables. uploads/ is the only scope written here: "
                                "memory/ and state/ are refused."
                            ),
                        },
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                    },
                    "required": ["path", "content"],
                },
            ),
            handler=_data_write,
        ),
        ToolEntry(
            name="send_photo",
            schema=ToolSchema(
                name="send_photo",
                description=(
                    "Send an image to the owner's chat. "
                    "Preferred: use file_path to send a local file. "
                    "Legacy: use image_base64 with raw base64 or __last_screenshot__."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Local file path to image (preferred)"},
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image data, or __last_screenshot__ to send the latest browser screenshot.",
                        },
                        "caption": {"type": "string", "description": "Optional caption for the photo"},
                    },
                    "required": [],
                },
            ),
            handler=_send_photo,
        ),
    ]
