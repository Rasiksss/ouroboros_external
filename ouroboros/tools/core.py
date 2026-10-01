"""File and data tools."""

from __future__ import annotations

import ast
import base64
import bisect
import json
import logging
import pathlib
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.skill_payload_policy import (
    SKILL_OWNER_STATE_FILENAMES,
    SKILL_PAYLOAD_BUCKETS,
    SKILL_PAYLOAD_CONTROL_DIRNAMES,
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    SkillPayloadPathError,
    cross_skill_redirect_error,
    decide_payload_short_form,
    resolve_skill_payload_target,
)
from ouroboros.contracts.task_constraint import TaskConstraint, normalize_task_constraint, resolve_payload_path
from ouroboros.utils import (
    _configured_data_dir,
    atomic_write_json,
    drive_aware_exists,
    read_text,
    safe_relpath,
    utc_now_iso,
    write_text,
)

log = logging.getLogger(__name__)

_MEMORY_AT_DRIVE_MEMORY = frozenset(
    {
        "identity.md",
        "scratchpad.md",
        "deep_review.md",
    }
)

# Payload-local provenance sidecars are marketplace-owned, not
# skill-author-editable. Generic write/delete/upload paths must block them.
_SELF_AUTHORED_MARKER = ".self_authored.json"
_DATA_PROVENANCE_SUFFIX = ".metadata.json"
_HIDDEN_DATA_PREFIXES = ("state/agent_notes/", "state/tmp/")


def _metadata_path_for_data_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + _DATA_PROVENANCE_SUFFIX)


def _data_artifact_purpose(rel: str) -> str:
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    if norm.startswith("state/agent_notes/"):
        return "agent_scratch"
    if norm.startswith("skills/"):
        return "skill_payload"
    return "artifact"


def _is_hidden_data_listing_item(item: str) -> bool:
    norm = str(item or "").replace("\\", "/").lstrip("./")
    return norm.endswith(_DATA_PROVENANCE_SUFFIX) or any(norm.startswith(prefix) for prefix in _HIDDEN_DATA_PREFIXES)


def _read_data_provenance(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = read_text(_metadata_path_for_data_path(path))
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_data_provenance(ctx: ToolContext, path: pathlib.Path, rel: str) -> None:
    payload = {
        "schema_version": 1,
        "origin": "agent_generated",
        "purpose": _data_artifact_purpose(rel),
        "context_eligible": False,
        "authoritative": False,
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "chat_id": getattr(ctx, "current_chat_id", None),
        "created_at": utc_now_iso(),
        "created_by_tool": "data_write",
    }
    atomic_write_json(_metadata_path_for_data_path(path), payload, trailing_newline=True)


def render_line_slice(
    path: str,
    content: str,
    max_lines: int = 2000,
    start_line: int = 1,
    *,
    start_char: int | None = None,
    max_chars: int | None = None,
) -> str:
    """Return a line-ranged file view with the shared read-tool header."""
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
        return line_result

    total_chars = len(content)
    complete_header = (
        f"# {path} — characters {absolute_start + 1}\u2013{absolute_end} "
        f"of {total_chars}; starts at line {start} of {total}\n"
    )
    complete_result = complete_header + body + line_footer
    if max_chars is None or len(complete_result) <= max_chars:
        return complete_result

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
    return header + content[absolute_start:page_end] + footer


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


#: Facts, knowledge topics, and identity are one store reached through one set
#: of tools, so they all name the same pair.
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

#: Every leftover the cutover wrote or stopped writing, by exact path. The
#: agent reaches for the scratchpad and identity by their root-level aliases as
#: readily as by their ``memory/`` paths, and the JSON/JSONL blobs beside them
#: are backfill sources and pre-cutover journals — stale by construction, and
#: previously served in full because ``_is_cognitive_data_path`` is true for
#: them.
_MIGRATED_MEMORY_FILES: dict[str, _MigratedOwner] = {
    "memory/identity.md": _MEMORY_OWNER,
    "identity.md": _MEMORY_OWNER,
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


def _skill_payload_parts(target: pathlib.Path, data_root: pathlib.Path) -> tuple[str, str, pathlib.Path] | None:
    """Return (bucket, skill, payload_root) for data/skills payload paths."""
    for candidate in (target, pathlib.Path(target).resolve(strict=False)):
        try:
            rel = candidate.relative_to(data_root)
        except (OSError, ValueError):
            continue
        parts = rel.parts
        if len(parts) < 3 or parts[0].lower() != "skills":
            continue
        bucket = parts[1]
        if bucket.lower() not in SKILL_PAYLOAD_BUCKETS:
            continue
        skill_name = parts[2]
        if not skill_name or skill_name in {".", ".."}:
            continue
        return bucket.lower(), skill_name, data_root / "skills" / bucket / skill_name
    return None


def _data_skill_path(path: str, drive_root: pathlib.Path) -> pathlib.Path | None:
    try:
        return resolve_skill_payload_target(pathlib.Path(drive_root), path).target_path
    except SkillPayloadPathError:
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


def _is_skill_owner_state_target(target: pathlib.Path, data_root: pathlib.Path) -> bool:
    if target.name.lower() not in SKILL_OWNER_STATE_FILENAMES:
        return False
    try:
        rel_to_data = target.relative_to(data_root)
        parts = rel_to_data.parts
        if len(parts) == 4 and parts[0].lower() == "state" and parts[1].lower() == "skills":
            return True
    except (OSError, ValueError):
        pass
    try:
        rel_to_data = target.resolve(strict=False).relative_to(data_root)
        parts = rel_to_data.parts
        if len(parts) == 4 and parts[0].lower() == "state" and parts[1].lower() == "skills":
            return True
    except (OSError, ValueError):
        pass
    skills_state_root = data_root / "state" / "skills"
    if not skills_state_root.is_dir():
        return False
    try:
        target_parent = target.parent.resolve(strict=False)
    except OSError:
        return False
    for skill_state_dir in skills_state_root.iterdir():
        try:
            if skill_state_dir.resolve(strict=False) == target_parent:
                return True
        except OSError:
            continue
    return False


def is_skill_control_plane_path(target: pathlib.Path, data_root: pathlib.Path) -> bool:
    """Return True for skill owner/provenance files blocked from generic writes."""
    if _is_skill_owner_state_target(target, data_root):
        return True

    def _matches_payload(candidate: pathlib.Path) -> bool:
        try:
            rel = candidate.relative_to(data_root)
        except (OSError, ValueError):
            return False
        parts = rel.parts
        # skills/<bucket>/<skill>/<filename> has at least 4 parts.
        if len(parts) < 4:
            return False
        if parts[0].lower() != "skills":
            return False
        if parts[1].lower() not in SKILL_PAYLOAD_BUCKETS:
            return False
        rel_tail = [part.lower() for part in parts[3:]]
        if any(part in SKILL_PAYLOAD_CONTROL_DIRNAMES for part in rel_tail):
            return True
        return candidate.name.lower() in SKILL_PAYLOAD_CONTROL_FILENAMES

    if _matches_payload(target):
        return True

    # Resolve symlinks so benign-looking paths to protected sidecars trip the guard.
    try:
        resolved = pathlib.Path(target).resolve(strict=False)
    except OSError:
        resolved = pathlib.Path(target)
    if _matches_payload(resolved):
        return True

    # Hardlink/inode defense for benign basenames pointing at protected sidecars.
    try:
        if not pathlib.Path(target).exists():
            return False
        rel = pathlib.Path(target).resolve(strict=False).relative_to(data_root)
        parts = rel.parts
        if len(parts) < 4 or parts[0].lower() != "skills" or parts[1].lower() not in SKILL_PAYLOAD_BUCKETS:
            return False
        payload_root = data_root / parts[0] / parts[1] / parts[2]
        for protected in payload_root.iterdir():
            if protected.name.lower() not in SKILL_PAYLOAD_CONTROL_FILENAMES:
                continue
            try:
                if protected.exists() and pathlib.Path(target).samefile(protected):
                    return True
            except OSError:
                continue
    except (OSError, ValueError):
        return False
    return False


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> list[str]:
    norm_rel = safe_relpath(rel)
    try:
        from ouroboros.storage.blob.drive import get_drive_storage

        configured = _configured_data_dir()
        root_resolved = pathlib.Path(root).resolve()
        on_data_plane = (
            root_resolved == configured or configured in root_resolved.parents or root_resolved in configured.parents
        )
        if on_data_plane:
            drive = get_drive_storage(configured)
            if root_resolved == configured:
                list_rel = "" if norm_rel in (".", "") else norm_rel
            else:
                base = root_resolved.relative_to(configured).as_posix()
                list_rel = base if norm_rel in (".", "") else f"{base}/{norm_rel}".strip("/")
            try:
                raw_entries = drive.list_entries(list_rel)
                entries = [entry for entry in raw_entries if not _is_hidden_data_listing_item(entry)]
                if entries:
                    items = entries[:max_entries]
                    if len(entries) > max_entries:
                        items.append(f"...(truncated at {max_entries})")
                    return items
                if raw_entries:
                    return []
                if list_rel:
                    return [f"⚠️ Directory not found: {rel}"]
            except ValueError:
                pass
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
    return norm


def _data_read(ctx: ToolContext, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Read a drive text file; duplicate drive_root prefixes are stripped."""
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    norm = _normalize_data_read_path(ctx, path)
    # Refused before any target is resolved, so no branch downstream can answer
    # a confined task's path from a process-wide memory service.
    migrated = _migrated_memory_owner(norm)
    if migrated is not None:
        return f"⚠️ DATA_READ_BLOCKED: not readable through generic data_read — {migrated.read_instead}."
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            target = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, norm)
        except ValueError as e:
            return f"⚠️ DATA_READ_BLOCKED: {e}"
    else:
        target = ctx.drive_path(norm)
    if _is_skill_owner_state_target(target, pathlib.Path(ctx.drive_root)) and target.name.lower() != "review.json":
        return "DATA_READ_BLOCKED: skill owner state is not readable through generic data tools."
    try:
        content = read_text(target)
        provenance = _read_data_provenance(target)
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
    # Refused ahead of the confinement branch, matching _data_read: a listing
    # that disagreed with the owning tool would be worse than no listing.
    migrated = _migrated_memory_owner(dir)
    if migrated is not None:
        return json.dumps(
            [f"⚠️ DATA_LIST_BLOCKED: not listable through generic data_list — {migrated.list_instead}."],
            ensure_ascii=False,
            indent=2,
        )
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            root = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, dir)
        except ValueError as e:
            return json.dumps([f"⚠️ DATA_LIST_BLOCKED: {e}"], ensure_ascii=False, indent=2)
        return json.dumps(_list_dir(root, ".", max_entries), ensure_ascii=False, indent=2)
    # A parent such as memory/ still enumerates: the unmigrated files there are
    # readable, and descending into a migrated subdirectory — or naming a
    # migrated file — is refused above rather than filtered out here.
    return json.dumps(_list_dir(ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2)


def _data_write(
    ctx: ToolContext,
    path: str,
    content: str,
    mode: str = "overwrite",
    bucket: str = "",
    skill_name: str = "",
) -> str:
    # bucket+skill_name synthesize a payload-confined skill_repair constraint.
    short_form = decide_payload_short_form(
        bucket=bucket,
        skill_name=skill_name,
        path_text=path,
        repo_dir=pathlib.Path(ctx.repo_dir),
        drive_root=pathlib.Path(ctx.drive_root),
    )
    if short_form.error:
        return f"⚠️ DATA_WRITE_ERROR: {short_form.error}"
    synth = short_form.constraint
    existing_tc = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    redirect_err = cross_skill_redirect_error(existing_tc, synth)
    if redirect_err:
        return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
    # Real skill_repair confinement wins over synthesized short-form context.
    if existing_tc and existing_tc.mode == "skill_repair":
        task_constraint: TaskConstraint | None = existing_tc
    else:
        task_constraint = synth or existing_tc
    write_path = _normalize_data_read_path(ctx, path)
    # Provenance sidecars are runtime-managed: a tool-authored one would let the
    # agent mark its own artifact authoritative and context-eligible.
    if write_path.lower().endswith(_DATA_PROVENANCE_SUFFIX):
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
            "⚠️ DATA_WRITE_BLOCKED: managed memory paths must be updated via "
            "memory_save/memory_delete, not generic data_write."
        )
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            p = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, path)
        except ValueError as e:
            return f"⚠️ DATA_WRITE_ERROR: {e}"
    else:
        explicit_skill_target = _data_skill_path(path, pathlib.Path(ctx.drive_root))
        p = explicit_skill_target if explicit_skill_target is not None else ctx.drive_path(write_path)
    # Defense-in-depth: settings.json is owner-only. Use inode-aware matching
    # for symlinks/hardlinks/case-insensitive APFS/NTFS, with a fallback for
    # not-yet-existing case variants.
    from ouroboros.app import config as _cfg

    target_path = pathlib.Path(p)
    settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
    data_root = pathlib.Path(_cfg.DATA_DIR).resolve(strict=False)
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        lexical_target = pathlib.Path(p).resolve(strict=False)
    else:
        lexical_target = pathlib.Path(ctx.drive_root).resolve(strict=False) / safe_relpath(write_path)
    suffix = pathlib.PurePosixPath(str(path or "")).suffix.lower()
    if suffix in {".py", ".md", ".json", ".sh"} and _looks_like_serialized_tool_result(content):
        return (
            "⚠️ DATA_WRITE_BLOCKED: content looks like a serialized tool result "
            "object (for example {'content': ...}) rather than file text. "
            "Extract the actual file body before calling data_write."
        )
    skill_owner_state_path = _is_skill_owner_state_target(lexical_target, data_root) or _is_skill_owner_state_target(
        target_path, data_root
    )
    if not skill_owner_state_path:
        skills_state_root = pathlib.Path(_cfg.DATA_DIR) / "state" / "skills"
        if target_path.exists() and skills_state_root.is_dir():
            for owner_state_file in skills_state_root.glob("*/*"):
                if owner_state_file.name.lower() not in SKILL_OWNER_STATE_FILENAMES:
                    continue
                try:
                    if owner_state_file.exists() and target_path.samefile(owner_state_file):
                        skill_owner_state_path = True
                        break
                except OSError:
                    continue
    if skill_owner_state_path:
        return (
            "⚠️ DATA_WRITE_BLOCKED: skill enablement and grant "
            "state are owner-controlled. Edit the skill payload under "
            "data/skills/ and use the owner/admin skill surface for enablement."
        )
    if is_skill_control_plane_path(lexical_target, data_root) or is_skill_control_plane_path(target_path, data_root):
        return (
            "⚠️ DATA_WRITE_BLOCKED: provenance markers (for example "
            "SKILL.openclaw.md) are owner-controlled. Edit user-authored "
            "payload files instead."
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
    marker_payload = _skill_payload_parts(lexical_target, data_root) or _skill_payload_parts(target_path, data_root)
    should_mark_self_authored = False
    marker_path: pathlib.Path | None = None
    if (
        mode == "overwrite"
        and not (task_constraint and task_constraint.mode == "skill_repair")
        and marker_payload is not None
        and marker_payload[0] == "external"
        and pathlib.PurePosixPath(str(path or "")).name.lower() in {"skill.md", "skill.json"}
        and not target_path.exists()
    ):
        marker_path = marker_payload[2] / _SELF_AUTHORED_MARKER
        should_mark_self_authored = not marker_path.exists()

    write_parts = pathlib.PurePosixPath(write_path).parts
    user_artifact_created = False
    if write_parts and write_parts[0] == "uploads":
        try:
            user_artifact_created = not drive_aware_exists(p)
        except Exception as exc:
            log.debug("User artifact existence probe failed exception_type=%s", type(exc).__name__)
    if mode == "overwrite":
        write_text(p, content)
    else:
        from ouroboros.storage.blob.drive import get_drive_storage

        drive = get_drive_storage(pathlib.Path(ctx.drive_root))
        rel = drive.rel_from_path(p)
        if rel is not None:
            existing = ""
            if drive.path_exists(p):
                try:
                    existing = read_text(p)
                except FileNotFoundError:
                    existing = ""
            write_text(p, existing + content)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
    try:
        _write_data_provenance(ctx, p, write_path)
    except Exception:
        log.debug("Failed to write data provenance for %s", write_path, exc_info=True)
    if should_mark_self_authored and marker_path is not None and marker_payload is not None:
        from ouroboros.skills.loader import compute_content_hash

        marker_skill_dir = marker_payload[2]
        marker_skill_dir.mkdir(parents=True, exist_ok=True)
        try:
            initial_hash = compute_content_hash(marker_skill_dir)
        except Exception:
            initial_hash = ""
        marker_payload_data = {
            "schema_version": 1,
            "origin": "self_authored",
            "created_at": utc_now_iso(),
            "chat_id": getattr(ctx, "current_chat_id", None),
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "created_by_tool": "data_write",
            "initial_content_hash": initial_hash,
        }
        atomic_write_json(marker_path, marker_payload_data, trailing_newline=True)
        state_marker = pathlib.Path(ctx.drive_root) / "state" / "skills" / marker_payload[1] / "self_authored.json"
        state_marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_marker, marker_payload_data, trailing_newline=True)
    if user_artifact_created:
        safe_filename = re.sub(r"[\x00-\x1f\x7f\s]+", "_", write_parts[-1])[:200] or "unknown"
        log.info(
            "event=user_artifact_created task_id=%s scope=uploads filename=%s size=%s",
            getattr(ctx, "task_id", "") or "unknown",
            safe_filename,
            len(content.encode("utf-8")),
        )
    result = f"OK: wrote {mode} {path} ({len(content)} chars)"
    if short_form.ignored_reason:
        result += f"\n⚠️ SKILL_SHORT_FORM_IGNORED: {short_form.ignored_reason}."
    return result


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _send_photo(ctx: ToolContext, file_path: str = "", image_base64: str = "", caption: str = "") -> str:
    """Compatibility alias for the canonical image delivery path."""
    if file_path:
        from ouroboros.tools.service.messaging import _send_image

        return _send_image(ctx, file_path=file_path, caption=caption)
    if not image_base64:
        return "⚠️ Provide either file_path or image_base64."
    actual_b64 = image_base64
    if image_base64 == "__last_screenshot__":
        actual_b64 = str(getattr(ctx.browser_state, "last_screenshot_b64", "") or "")
        if not actual_b64:
            return "⚠️ No screenshot stored in browser state."
    from ouroboros.storage.chat_attachments import MAX_OUTBOUND_BYTES

    if len(actual_b64) > ((MAX_OUTBOUND_BYTES + 2) // 3) * 4:
        return f"⚠️ Image exceeds {MAX_OUTBOUND_BYTES // (1024 * 1024)} MB limit."
    try:
        data = base64.b64decode(actual_b64, validate=True)
    except ValueError:
        return "⚠️ Image base64 is invalid."
    mime = _detect_image_mime(data)
    suffix = {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(mime)
    if suffix is None:
        return "⚠️ Unsupported image format. Use PNG, JPEG, WEBP, or GIF."
    from ouroboros.tools.service.messaging import _send_image_bytes

    return _send_image_bytes(
        ctx,
        data,
        filename=f"image{suffix}",
        mime=mime,
        caption=caption,
    )


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
                    "(useful when the full content exceeds a single LLM output budget). "
                    "bucket+skill_name is only for an existing skill payload, not ordinary files."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to runtime DATA_DIR; use uploads/<filename> for deliverables.",
                        },
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                        "bucket": {
                            "type": "string",
                            "enum": ["external", "clawhub", "ouroboroshub"],
                            "description": "Skill payload bucket for short relative payload paths only. Pair with skill_name. Do not supply for explicit data paths.",
                        },
                        "skill_name": {
                            "type": "string",
                            "description": "Skill slug for short relative payload paths only. Requires bucket.",
                        },
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
