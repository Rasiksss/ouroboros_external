"""Trusted distribution skill discovery and manifest capability status.

Payloads are read only from ``OUROBOROS_REPO_DIR/skills``. Private extension
state may live under ``data/state/skills/<name>/``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import pathlib
import platform
import re
import shutil
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypedDict

from new_core.skills.interfaces import IExtensionRuntime, ISkillLoader
from new_core.skills.manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from new_core.skills.models import LoadedSkill, SkillBlockReason, SkillId, normalize_skill_alias
from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.skill_payload_policy import (
    SKILL_PAYLOAD_SKIPPED_NAMES,
    canonical_declared_script_rel,
    is_skill_presentation_file,
)
from ouroboros.skills.bundled import bundled_skills_root, inactive_bundled_skill_names

if TYPE_CHECKING:
    from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)


# Constants

_MANIFEST_NAMES = ("SKILL.md", "skill.json")
_PRIMARY_MANIFEST_NAME = "SKILL.md"
_MAX_SKILL_FILE_BYTES = 256 * 1024
_MAX_SKILL_TREE_BYTES = 20 * 1024 * 1024
_MAX_SKILL_FILE_COUNT = 200
_PAYLOAD_DIGEST_CACHE_SIZE = 2048

# Sensitive files are excluded from review prompts and hashes; their presence
# in a runtime-reachable skill tree is handled as a hard block below.

# Disk paths


def _skills_state_root(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "skills"


def skill_state_dir(drive_root: pathlib.Path, skill: SkillId) -> pathlib.Path:
    """Return the private state directory for a skill (created on demand).

    The runtime key is the only path-safe projection of a package identity.
    """
    safe = skill.runtime_key
    path = _skills_state_root(drive_root) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_skill_name(name: str) -> str:
    """Clamp a skill name to a safe on-disk identifier.

    Keep alphanumerics, dashes, underscores, and dots; replace everything
    else with ``_``. Empty / pathological inputs become ``"_unnamed"``.
    """
    return normalize_skill_alias(name) or "_unnamed"


def _distribution_local_id(skill_dir: pathlib.Path, source_root: pathlib.Path | None) -> str:
    if source_root is None:
        return _sanitize_skill_name(skill_dir.name)
    try:
        relative = skill_dir.relative_to(pathlib.Path(source_root).resolve())
    except ValueError:
        return _sanitize_skill_name(skill_dir.name)
    return "/".join(_sanitize_skill_name(part) for part in relative.parts) or _sanitize_skill_name(skill_dir.name)


# Manifest discovery


class _ManifestUnreadable(RuntimeError):
    """A manifest file exists but could not be read (permissions,
    truncation, IO error, etc.). Callers translate this into a
    ``LoadedSkill`` with ``load_error`` set so the broken skill is
    still visible in ``list_skills`` instead of silently disappearing
    from discovery."""

    def __init__(self, path: pathlib.Path, err: BaseException) -> None:
        super().__init__(f"manifest {path}: {type(err).__name__}: {err}")
        self.path = path
        self.err = err


def _manifest_text_for_dir(skill_dir: pathlib.Path) -> tuple[str, pathlib.Path] | None:
    """Return (manifest_text, manifest_path) for a skill dir.

    Returns ``None`` ONLY when the directory has no manifest at all
    (i.e. "this is not a skill dir"). A manifest that exists but can't
    be read raises ``_ManifestUnreadable`` so the caller can surface
    the broken skill with a ``load_error`` instead of pretending the
    dir was not a skill dir in the first place.
    """
    for candidate in _MANIFEST_NAMES:
        mf = skill_dir / candidate
        if mf.is_file():
            try:
                return mf.read_text(encoding="utf-8"), mf
            except (OSError, UnicodeDecodeError) as exc:
                # Catch BOTH IO failures and decode failures: a manifest
                # with invalid UTF-8 would otherwise crash discovery for
                # the whole skills checkout instead of degrading to a
                # single broken-skill entry.
                log.warning("Failed to read skill manifest %s", mf, exc_info=True)
                raise _ManifestUnreadable(mf, exc) from exc
    return None


def skill_payload_files(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: list[dict[str, Any]] | None = None,
) -> list[pathlib.Path]:
    """Return files hashed for review freshness.

    The hash covers every regular runtime-reachable file under ``skill_dir``
    except metadata/cache/sensitive paths and symlink escapes. Manifest entry
    points are re-added only when confined, keeping executable and reviewed
    surfaces aligned.
    """
    out: list[pathlib.Path] = []
    resolved_root = skill_dir.resolve()

    def _add(path: pathlib.Path) -> None:
        if path not in out:
            out.append(path)

    def _add_if_confined(relpath: str) -> None:
        rel = str(relpath or "").strip()
        if not rel or rel.startswith(("/", "~")):
            return
        if ".." in pathlib.PurePosixPath(rel).parts:
            return
        resolved = (skill_dir / rel).resolve()
        try:
            if is_skill_presentation_file(resolved.relative_to(resolved_root).as_posix()):
                return
        except ValueError:
            return
        if resolved.is_file():
            _add(resolved)

    # Broad walk: everything runtime-reachable, minus metadata/cache names.
    # Every candidate is resolved back under skill_dir so symlinks cannot leak
    # outside files into reviewer prompts. Sensitive-path policy is shared with
    # repo review.
    from ouroboros.skills.repo_pack import is_sensitive_path

    if resolved_root.is_dir():
        for path in sorted(resolved_root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(resolved_root).parts
            except ValueError:
                continue
            if is_skill_presentation_file("/".join(rel_parts)):
                continue
            if any(part in SKILL_PAYLOAD_SKIPPED_NAMES for part in rel_parts):
                continue
            if is_sensitive_path(path.name):
                # Fail closed: a reviewed skill could still read a skipped
                # credential-shaped file at runtime.
                raise SkillPayloadUnreadable(
                    str(path.relative_to(resolved_root)),
                    RuntimeError(
                        "sensitive-shape filename present in skill tree "
                        "(e.g. .env / credentials.json / .pem). Rename "
                        "or relocate the file outside the skill checkout."
                    ),
                )
            # Symlink escape guard: resolve the final path and re-check
            # confinement under skill_dir.
            try:
                real = path.resolve()
            except (OSError, RuntimeError):
                log.warning("Could not resolve skill file %s", path, exc_info=True)
                continue
            try:
                if is_skill_presentation_file(real.relative_to(resolved_root).as_posix()):
                    continue
            except ValueError:
                log.warning(
                    "Skill file %s resolves outside skill_dir (%s) — excluded from review pack.",
                    path,
                    resolved_root,
                )
                continue
            _add(path)

    # Add manifest-declared entry/scripts explicitly after confinement checks.
    _add_if_confined(manifest_entry)
    for script_entry in manifest_scripts or []:
        if not isinstance(script_entry, dict):
            continue
        declared_name = str(script_entry.get("name") or "").strip()
        if not declared_name:
            continue
        _add_if_confined(declared_name)
        if "/" not in declared_name:
            _add_if_confined(f"scripts/{declared_name}")

    out.sort()
    return out


class SkillPayloadUnreadable(RuntimeError):
    """Raised when the skill hash cannot cover the full runtime surface."""

    def __init__(self, relpath: str, err: BaseException) -> None:
        super().__init__(f"Skill payload {relpath!r} unreadable: {type(err).__name__}: {err}")
        self.relpath = relpath
        self.err = err


def _payload_file_fingerprint(path: pathlib.Path) -> tuple[int, int, int, int, int, int]:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@functools.lru_cache(maxsize=_PAYLOAD_DIGEST_CACHE_SIZE)
def _payload_file_digest(
    path: pathlib.Path,
    fingerprint: tuple[int, int, int, int, int, int],
) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if _payload_file_fingerprint(path) != fingerprint:
        raise OSError("skill payload changed while hashing")
    return digest.digest()


def compute_content_hash(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: list[dict[str, Any]] | None = None,
) -> str:
    """Compute the review-staleness hash for manifest plus payload files.

    Unreadable payload files fail closed via :class:`SkillPayloadUnreadable`
    so callers never emit a PASS over a partial runtime surface.
    """
    digest = hashlib.sha256()
    skill_dir = skill_dir.resolve()
    for file_path in skill_payload_files(
        skill_dir,
        manifest_entry=manifest_entry,
        manifest_scripts=manifest_scripts,
    ):
        rel = file_path.relative_to(skill_dir).as_posix()
        try:
            fingerprint = _payload_file_fingerprint(file_path)
            file_digest = _payload_file_digest(file_path, fingerprint)
        except OSError as exc:
            log.warning("Failed to read skill payload file %s", file_path, exc_info=True)
            raise SkillPayloadUnreadable(rel, exc) from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest)
    return digest.hexdigest()


def requested_core_setting_keys(env_keys: list[str]) -> list[str]:
    """Return protected settings declared by a skill manifest."""
    forbidden_upper = {key.upper() for key in FORBIDDEN_SKILL_SETTINGS}
    requestable_when_absent = {"TELEGRAM_BOT_TOKEN"}
    try:
        from ouroboros.app.config import SETTINGS_DEFAULTS, load_settings

        settings = load_settings()
        custom_secret_upper = {
            str(key).upper()
            for key in settings
            if str(key).upper() not in SETTINGS_DEFAULTS and str(key).upper().replace("_", "").isalnum()
        }
    except Exception:
        custom_secret_upper = set()
    out: list[str] = []
    for raw_key in env_keys or []:
        key = str(raw_key or "").strip().upper()
        if (
            key
            and (key in forbidden_upper or key in custom_secret_upper or key in requestable_when_absent)
            and key not in out
        ):
            out.append(key)
    return out


_TRUSTED_SKILL_CAPABILITIES = frozenset(
    {
        "inject_chat",
        "subscribe_event:chat.outbound",
        "subscribe_event:chat.typing",
        "subscribe_event:chat.photo",
    }
)


def requested_skill_permissions(
    permissions: list[str],
    subscribe_events: list[str] | None = None,
) -> list[str]:
    """Return the complete manifest capability set for status and authorization."""
    permission_set = {str(item or "").strip() for item in (permissions or [])}
    requested = sorted(permission for permission in permission_set if permission)
    if "subscribe_event" in permission_set:
        for raw_topic in subscribe_events or []:
            topic = str(raw_topic or "").strip()
            capability = f"subscribe_event:{topic}"
            if capability in _TRUSTED_SKILL_CAPABILITIES and capability not in requested:
                requested.append(capability)
    return sorted(requested)


def trusted_setting_keys_for_skill(skill: LoadedSkill) -> list[str]:
    if not skill.distribution_owned:
        return []
    return requested_core_setting_keys(list(skill.manifest.env_from_settings or []))


def trusted_permissions_for_skill(skill: LoadedSkill) -> list[str]:
    return list(skill.trusted_permissions)


def _available_for_execution(skill: LoadedSkill) -> bool:
    if not skill.selected or not skill.eligible or skill.load_error or not skill.manifest.is_script():
        return False
    from ouroboros.tools.skill_exec import _resolve_runtime_binary, _resolve_script_path

    runtime = (skill.manifest.runtime or "").strip().lower()
    if _resolve_runtime_binary(runtime) is None:
        return False
    for entry in skill.manifest.scripts:
        relpath = canonical_declared_script_rel(entry.get("name", ""))
        if relpath is not None and _resolve_script_path(skill.skill_dir, relpath.as_posix()) is not None:
            return True
    return False


# Discovery / loading


def load_skill(
    skill_dir: pathlib.Path,
    *,
    source_root: pathlib.Path | None = None,
    source_kind: str = "",
    precedence: int = 0,
) -> LoadedSkill | None:
    """Load one skill, returning ``None`` only when no manifest exists."""
    skill_dir = skill_dir.resolve()
    local_id = _distribution_local_id(skill_dir, source_root)
    try:
        manifest_read = _manifest_text_for_dir(skill_dir)
    except _ManifestUnreadable as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            skill_id=SkillId("distribution", local_id),
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest unreadable: {exc}",
            skill_path=exc.path,
            source_root=source_root,
            source_kind=source_kind,
            precedence=precedence,
        )
    if manifest_read is None:
        return None
    manifest_text, manifest_path = manifest_read

    try:
        manifest = parse_skill_manifest_text(manifest_text)
    except SkillManifestError as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            skill_id=SkillId("distribution", local_id),
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest parse error: {exc}",
            skill_path=manifest_path,
            source_root=source_root,
            source_kind=source_kind,
            precedence=precedence,
        )

    # Runtime/state/tool identity is the source-relative directory path. manifest.name is
    # display metadata and may be localized or renamed.
    if not manifest.name:
        manifest.name = skill_dir.name

    name = _sanitize_skill_name(skill_dir.name)
    load_error = ""
    try:
        content_hash = compute_content_hash(
            skill_dir,
            manifest_entry=manifest.entry,
            manifest_scripts=manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        content_hash = ""
        load_error = f"payload unreadable: {exc}"
    skill_id = SkillId("distribution", local_id)
    return LoadedSkill(
        skill_id=skill_id,
        name=name,
        skill_dir=skill_dir,
        manifest=manifest,
        content_hash=content_hash,
        load_error=load_error,
        skill_path=manifest_path,
        source_root=source_root,
        source_kind=source_kind,
        precedence=precedence,
    )


def _settings_get(settings: Any, key: str, default: Any = None) -> Any:
    if isinstance(settings, dict):
        return settings.get(key, default)
    getter = getattr(settings, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(settings, key, default)


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _openclaw_metadata(manifest: SkillManifest) -> dict[str, Any]:
    metadata = manifest.raw_extra.get("metadata") if isinstance(manifest.raw_extra, dict) else None
    if not isinstance(metadata, dict):
        return {}
    openclaw = metadata.get("openclaw")
    return openclaw if isinstance(openclaw, dict) else {}


def _setting_path_value(settings: Any, dotted: str) -> Any:
    if not dotted:
        return None
    direct = _settings_get(settings, dotted, None)
    if direct is not None:
        return direct
    if not isinstance(settings, dict):
        return None
    current: Any = settings
    for part in dotted.split("."):
        if not part or not isinstance(current, dict) or part not in current:
            return None
        current = current.get(part)
    return current


def _current_os_tokens() -> set[str]:
    raw = platform.system().strip().lower()
    tokens = {raw}
    if raw == "darwin":
        tokens.update({"mac", "macos", "osx"})
    elif raw == "windows":
        tokens.add("win32")
    return tokens


def _is_bundled_root(root: pathlib.Path) -> bool:
    bundled = bundled_skills_root()
    return bundled is not None and root.resolve(strict=False) == bundled


def _resolved_path_allowed(
    path: pathlib.Path,
    root: pathlib.Path,
) -> bool:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _discover_skill_dirs(
    root: pathlib.Path,
) -> tuple[list[pathlib.Path], list[dict[str, str]]]:
    dirs: list[pathlib.Path] = []
    skipped: list[dict[str, str]] = []
    if not root.is_dir():
        return dirs, skipped
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = pathlib.Path(current_root)
        rel = current.relative_to(root)
        if rel.parts:
            dirnames[:] = [
                name for name in dirnames if not name.startswith(".") and name not in SKILL_PAYLOAD_SKIPPED_NAMES
            ]
        else:
            dirnames[:] = [name for name in dirnames if name not in SKILL_PAYLOAD_SKIPPED_NAMES]
        if _PRIMARY_MANIFEST_NAME not in filenames:
            continue
        skill_path = current / _PRIMARY_MANIFEST_NAME
        if not _resolved_path_allowed(skill_path, root):
            skipped.append(
                {
                    "name": current.name,
                    "location": str(skill_path),
                    "reason": "path escapes allowed skill roots via symlink or containment violation",
                }
            )
            dirnames[:] = []
            continue
        dirs.append(current)
        dirnames[:] = []
    return sorted(dirs), skipped


def disabled_skill_ids(settings: AppSettings) -> frozenset[str]:
    """Canonical ids dropped from discovery via ``OUROBOROS_DISABLED_SKILLS``."""
    raw = str(_settings_get(settings, "OUROBOROS_DISABLED_SKILLS", "") or "")
    values = tuple(name for name in re.split(r"[\s,]+", raw) if name)
    for value in values:
        try:
            SkillId.parse(value)
        except ValueError as exc:
            suggestion = f"distribution:{value}" if ":" not in value else "source_id:local_id"
            raise ValueError(
                "OUROBOROS_DISABLED_SKILLS accepts only canonical source-qualified skill ids; "
                f"replace {value!r} with {suggestion!r}"
            ) from exc
    return frozenset(values)


def _skill_tree_stats(skill_dir: pathlib.Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    try:
        files = skill_payload_files(skill_dir)
    except SkillPayloadUnreadable:
        return 0, 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
            file_count += 1
        except OSError:
            continue
    return file_count, total_bytes


def _skill_frontmatter_error(skill: LoadedSkill) -> str:
    path = skill.skill_path or (skill.skill_dir / _PRIMARY_MANIFEST_NAME)
    if path.name != _PRIMARY_MANIFEST_NAME:
        return ""
    try:
        raw = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except OSError as exc:
        return f"frontmatter error: unreadable SKILL.md: {exc}"
    if not raw.startswith("---\n"):
        return "frontmatter error: SKILL.md must start with YAML frontmatter"
    lines = raw.splitlines()
    for line in lines[1:]:
        if line.strip() == "---":
            return ""
    return "frontmatter error: SKILL.md frontmatter is missing a closing --- delimiter"


def _apply_skill_validation(
    skill: LoadedSkill,
    settings: Any,
) -> None:
    reasons: list[SkillBlockReason] = []
    manifest_path = skill.skill_path or (skill.skill_dir / _PRIMARY_MANIFEST_NAME)
    if manifest_path.is_symlink():
        reasons.append(SkillBlockReason("invalid_package", "skill manifest must be a regular file, not a symlink"))
    frontmatter_error = _skill_frontmatter_error(skill)
    if frontmatter_error:
        reasons.append(SkillBlockReason("invalid_package", frontmatter_error))
        if not skill.load_error:
            skill.load_error = frontmatter_error
    if not skill.manifest.description:
        reasons.append(SkillBlockReason("invalid_package", "missing required frontmatter field: description"))
    if skill.load_error:
        reasons.append(SkillBlockReason("invalid_package", skill.load_error))

    openclaw = _openclaw_metadata(skill.manifest)
    raw_requires = openclaw.get("requires")
    requires: dict[str, Any] = raw_requires if isinstance(raw_requires, dict) else {}
    allowed_os = _normalize_list(openclaw.get("os"))
    if allowed_os and not (_current_os_tokens() & {item.lower() for item in allowed_os}):
        reasons.append(SkillBlockReason("unsupported_os", f"os gate failed: requires one of {allowed_os}"))
    for binary in _normalize_list(requires.get("bins")):
        if shutil.which(binary) is None:
            reasons.append(SkillBlockReason("missing_executable", f"missing required bin: {binary}"))
    for key in _normalize_list(requires.get("env")):
        if not str(_setting_path_value(settings, key) or "").strip():
            reasons.append(SkillBlockReason("missing_configuration", f"missing required env: {key}"))
    for key in _normalize_list(requires.get("config")):
        if not _setting_path_value(settings, key):
            reasons.append(SkillBlockReason("missing_configuration", f"missing required config: {key}"))

    file_count, total_bytes = _skill_tree_stats(skill.skill_dir)
    if file_count > _MAX_SKILL_FILE_COUNT:
        reasons.append(
            SkillBlockReason(
                "package_too_large", f"oversized skill tree: {file_count} files exceeds {_MAX_SKILL_FILE_COUNT}"
            )
        )
    if total_bytes > _MAX_SKILL_TREE_BYTES:
        reasons.append(
            SkillBlockReason(
                "package_too_large", f"oversized skill tree: {total_bytes} bytes exceeds {_MAX_SKILL_TREE_BYTES}"
            )
        )
    try:
        skill_bytes = pathlib.Path(skill.location).stat().st_size
    except OSError:
        skill_bytes = 0
    if skill_bytes > _MAX_SKILL_FILE_BYTES:
        reasons.append(
            SkillBlockReason(
                "package_too_large", f"oversized SKILL.md: {skill_bytes} bytes exceeds {_MAX_SKILL_FILE_BYTES}"
            )
        )

    if skill.shadowed_by:
        reasons.append(
            SkillBlockReason("invalid_package", f"shadowed by higher-precedence copy at {skill.shadowed_by}")
        )
    if skill.manifest.is_tool():
        entry = str(skill.manifest.entry or "tool.py").strip() or "tool.py"
        entry_path = pathlib.PurePosixPath(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            reasons.append(SkillBlockReason("invalid_package", "type=tool entry must stay inside the skill directory"))
        elif not (skill.skill_dir / entry).is_file():
            reasons.append(SkillBlockReason("invalid_package", f"type=tool requires entry file {entry}"))

    skill.blockers = list(dict.fromkeys(reasons))
    skill.eligible = skill.selected and not skill.blockers


def discover_distribution_skills(
    distribution_root: pathlib.Path,
    *,
    settings: AppSettings,
) -> list[LoadedSkill]:
    discovered: list[LoadedSkill] = []
    root = pathlib.Path(distribution_root).resolve(strict=False)
    hidden_bundled = inactive_bundled_skill_names(settings)
    disabled = disabled_skill_ids(settings)
    for precedence, source_root in enumerate((root,)):
        source_kind = "distribution"
        skill_dirs, skipped = _discover_skill_dirs(source_root)
        for item in skipped:
            name = _sanitize_skill_name(item["name"])
            local_id = _distribution_local_id(pathlib.Path(item["location"]).parent, source_root)
            discovered.append(
                LoadedSkill(
                    skill_id=SkillId("distribution", local_id),
                    name=name,
                    skill_dir=pathlib.Path(item["location"]).parent,
                    manifest=SkillManifest(name=name, description="", version="", type="instruction"),
                    content_hash="",
                    load_error=item["reason"],
                    source=source_kind,
                    skill_path=pathlib.Path(item["location"]),
                    source_root=source_root,
                    source_kind=source_kind,
                    precedence=precedence,
                    selected=False,
                    eligible=False,
                    blockers=[SkillBlockReason("invalid_package", item["reason"])],
                )
            )
        for skill_dir in skill_dirs:
            loaded = load_skill(
                skill_dir,
                source_root=source_root,
                source_kind=source_kind,
                precedence=precedence,
            )
            if loaded is None:
                continue
            if _skill_frontmatter_error(loaded):
                loaded.name = _sanitize_skill_name(skill_dir.name)
            else:
                loaded.name = _sanitize_skill_name(loaded.manifest.name or skill_dir.name)
            if str(loaded.skill_id) in disabled:
                log.info("Skill %s is not offered: disabled via OUROBOROS_DISABLED_SKILLS", loaded.skill_id)
                continue
            local_id = loaded.skill_id.local_id
            if local_id in hidden_bundled and _is_bundled_root(source_root):
                # Belongs to a feature that is switched off. Said out loud, because a skill
                # vanishing from discovery with no line anywhere is what makes this look
                # like a load failure.
                log.info(
                    "Bundled skill %s is not offered: the feature it belongs to is disabled",
                    loaded.name,
                )
                continue
            loaded.source = source_kind
            loaded.distribution_owned = source_kind == "distribution"
            if loaded.distribution_owned:
                loaded.trusted_permissions = tuple(
                    requested_skill_permissions(
                        list(loaded.manifest.permissions or []),
                        list(loaded.manifest.subscribe_events or []),
                    )
                )
            discovered.append(loaded)

    same_precedence: dict[tuple[str, int], list[LoadedSkill]] = {}
    for skill in discovered:
        same_precedence.setdefault((skill.skill_id.local_id, skill.precedence), []).append(skill)
    collision_keys = {key for key, group in same_precedence.items() if len(group) > 1}
    for key in collision_keys:
        group = same_precedence[key]
        locations = ", ".join(skill.location for skill in group)
        for skill in group:
            skill.selected = False
            skill.load_error = (
                f"Skill name collision: multiple skill directories at the same "
                f"precedence ({locations}) resolve to {skill.skill_id.local_id!r}."
            )

    winners: dict[str, LoadedSkill] = {}
    for skill in sorted(discovered, key=lambda item: (item.precedence, item.location)):
        local_id = skill.skill_id.local_id
        if (local_id, skill.precedence) in collision_keys:
            continue
        winners.setdefault(local_id, skill)
    for skill in discovered:
        winner = winners.get(skill.skill_id.local_id)
        if winner is not None and winner.location != skill.location:
            skill.selected = False
            skill.shadowed_by = winner.location
        _apply_skill_validation(skill, settings)

    discovered.sort(key=lambda item: (item.name, item.precedence, item.location))
    return discovered


# Status helpers consumed by the Skills UI


class SkillToolSurface(TypedDict):
    name: str
    description: str


class SkillSummaryRow(TypedDict):
    name: str
    skill_id: str
    description: str
    when_to_use: str
    type: str
    version: str
    location: str
    source_kind: str
    discovered: bool
    selected: bool
    eligible: bool
    live_loaded: bool
    ready: bool
    blocked_reason: str
    blocked_reasons: list[str]
    shadowed_by: str
    skill_key: str
    available_for_execution: bool
    runnable_via_skill_exec: bool
    tool_surfaces: list[SkillToolSurface]
    static_ready: bool
    load_error: str
    source: str


class SkillSummary(TypedDict):
    count: int
    selected: int
    eligible: int
    available: int
    broken: int
    skills: list[SkillSummaryRow]


class SkillCheckSummary(SkillSummary):
    invalid: int


def summarize_skills(
    skills: Sequence[LoadedSkill],
    *,
    extension_runtime: IExtensionRuntime | None = None,
) -> SkillSummary:
    """Return a compact catalogue summary for prompts and inspection tools."""
    tool_surfaces_by_skill: dict[str, list[SkillToolSurface]] = {}
    extension_states: dict[str, dict[str, Any]] = {}
    if extension_runtime is not None:
        try:
            for tool in extension_runtime.registered_tools():
                skill_name = str(tool.get("skill") or "")
                if not skill_name:
                    continue
                tool_surfaces_by_skill.setdefault(skill_name, []).append(
                    {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                    }
                )
            extension_states = {
                skill.runtime_key: extension_runtime.runtime_state(skill)
                for skill in skills
                if skill.manifest.is_extension()
            }
        except Exception:
            tool_surfaces_by_skill = {}
            extension_states = {}

    rows: list[SkillSummaryRow] = []
    for skill in skills:
        access_supported = not (
            (skill.manifest.env_from_settings or skill.manifest.permissions) and not skill.manifest.is_executable()
        )
        extension_state = extension_states.get(skill.runtime_key, {})
        live_loaded = bool(extension_state.get("live_loaded"))
        blocked_reason = ""
        if skill.load_error:
            blocked_reason = skill.load_error
        elif not skill.selected or not skill.eligible:
            blocked_reason = next(iter(skill.blocked_reasons), "not_eligible")
        elif not access_supported:
            blocked_reason = "capabilities_require_executable_skill"
        elif skill.manifest.is_extension() and not live_loaded:
            blocked_reason = str(extension_state.get("load_error") or "not_live_loaded")

        if skill.manifest.is_extension():
            ready = live_loaded and not blocked_reason
        elif skill.manifest.is_executable():
            ready = _available_for_execution(skill) and not blocked_reason
        else:
            ready = skill.selected and skill.eligible and not blocked_reason

        rows.append(
            {
                "name": skill.name,
                "skill_id": str(skill.skill_id),
                "description": skill.manifest.description,
                "when_to_use": skill.manifest.when_to_use,
                "type": skill.manifest.type,
                "version": skill.manifest.version,
                "location": skill.location,
                "source_kind": skill.source_kind or skill.source,
                "discovered": True,
                "selected": skill.selected,
                "eligible": skill.eligible,
                "live_loaded": live_loaded,
                "ready": ready,
                "blocked_reason": blocked_reason,
                "blocked_reasons": list(skill.blocked_reasons),
                "shadowed_by": skill.shadowed_by,
                "skill_key": skill.skill_key,
                "available_for_execution": _available_for_execution(skill),
                "runnable_via_skill_exec": _available_for_execution(skill),
                "tool_surfaces": tool_surfaces_by_skill.get(skill.runtime_key, []),
                "static_ready": _available_for_execution(skill),
                "load_error": skill.load_error,
                "source": skill.source,
            }
        )
    return {
        "count": len(skills),
        "selected": sum(1 for s in skills if s.selected),
        "eligible": sum(1 for s in skills if s.eligible),
        "available": sum(1 for row in rows if row["ready"]),
        "broken": sum(1 for s in skills if s.load_error),
        "skills": rows,
    }


def check_skills(
    skills: Sequence[LoadedSkill],
    *,
    extension_runtime: IExtensionRuntime | None = None,
) -> SkillCheckSummary:
    """Return deterministic discovery plus validation failures."""
    summary = summarize_skills(skills, extension_runtime=extension_runtime)
    return {
        **summary,
        "invalid": sum(1 for skill in summary["skills"] if not skill.get("eligible")),
    }


class DistributionSkillLoader(ISkillLoader):
    """Load repository-backed distribution packages using application policy."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def load(self, root: pathlib.Path) -> Sequence[LoadedSkill]:
        return discover_distribution_skills(root, settings=self._settings)


__all__ = [
    "DistributionSkillLoader",
    "SkillCheckSummary",
    "SkillSummary",
    "SkillSummaryRow",
    "SkillToolSurface",
    "check_skills",
    "compute_content_hash",
    "discover_distribution_skills",
    "load_skill",
    "requested_core_setting_keys",
    "skill_payload_files",
    "skill_state_dir",
    "summarize_skills",
    "trusted_permissions_for_skill",
    "trusted_setting_keys_for_skill",
]
