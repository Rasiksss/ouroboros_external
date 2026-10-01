"""Trusted distribution skill discovery and manifest capability status.

Payloads are read only from ``OUROBOROS_REPO_DIR/skills``. Private extension
state may live under ``data/state/skills/<name>/``; enable/grant files there do
not control distribution skills.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import pathlib
import platform
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.skill_manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.skills.bundled import (
    BUNDLED_ROOT_KIND,
    BUNDLED_SKILL_NAMES,
    bundled_skills_root,
    inactive_bundled_skill_names,
)
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

if TYPE_CHECKING:
    from ouroboros.storage.settings.schema import AppSettings

log = logging.getLogger(__name__)


def load_settings() -> AppSettings:
    from ouroboros.storage.settings.store import load_settings as _load_settings

    return _load_settings()


# Constants

_MANIFEST_NAMES = ("SKILL.md", "skill.json")
_OPENCLAW_MANIFEST_NAME = "SKILL.md"
_MAX_SKILL_FILE_BYTES = 256 * 1024
_MAX_SKILL_TREE_BYTES = 2 * 1024 * 1024
_MAX_SKILL_FILE_COUNT = 200
_PAYLOAD_DIGEST_CACHE_SIZE = 2048
# Only metadata/cache names are skipped. Non-metadata dotfiles remain hashed
# and reviewed because a skill subprocess can import/source/read them.
_SKILL_DIR_CACHE_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".tox",
        ".ouroboros_env",
        ".DS_Store",
    }
)

# Sensitive files are excluded from review prompts and hashes; their presence
# in a runtime-reachable skill tree is handled as a hard block below.

GRANTS_FILENAME = "grants.json"
SELF_AUTHORED_MARKER_FILENAME = ".self_authored.json"


# Dataclasses


@dataclass
class LoadedSkill:
    """Discovered distribution skill plus validation and runtime metadata."""

    name: str
    skill_dir: pathlib.Path
    manifest: SkillManifest
    content_hash: str
    enabled: bool = False
    load_error: str = ""
    source: str = "distribution"
    is_self_authored: bool = False
    skill_path: pathlib.Path | None = None
    source_root: pathlib.Path | None = None
    source_kind: str = ""
    distribution_owned: bool = False
    precedence: int = 0
    selected: bool = True
    eligible: bool = True
    blocked_reasons: list[str] = field(default_factory=list)
    shadowed_by: str = ""

    @property
    def available_for_execution(self) -> bool:
        """True when an enabled script skill can run via skill_exec."""
        if self.load_error:
            return False
        if not self.enabled:
            return False
        if not self.manifest.is_script():
            return False
        from ouroboros.tools.skill_exec import _resolve_runtime_binary, _resolve_script_path

        runtime = (self.manifest.runtime or "").strip().lower()
        if _resolve_runtime_binary(runtime) is None:
            return False
        for entry in self.manifest.scripts or []:
            if not isinstance(entry, dict):
                continue
            declared_name = str(entry.get("name") or "").strip()
            if not declared_name:
                continue
            relpath = (
                declared_name if "/" in declared_name or declared_name.startswith(".") else f"scripts/{declared_name}"
            )
            if _resolve_script_path(self.skill_dir, relpath) is not None:
                return True
        return False

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def location(self) -> str:
        path = self.skill_path
        if path is None:
            for candidate in _MANIFEST_NAMES:
                manifest_path = self.skill_dir / candidate
                if manifest_path.is_file():
                    path = manifest_path
                    break
        return str(path or (self.skill_dir / _OPENCLAW_MANIFEST_NAME))

    @property
    def skill_key(self) -> str:
        openclaw = _openclaw_metadata(self.manifest)
        return str(openclaw.get("skillKey") or "").strip() or self.name


# Disk paths


def _skills_state_root(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "skills"


def skill_state_dir(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    """Return ``~/Ouroboros/data/state/skills/<name>/`` (created on demand).

    The name is normalized to its alnum-dashes shape before joining so a
    malicious manifest ``name: ../foo`` cannot escape the state root.
    """
    safe = _sanitize_skill_name(name)
    path = _skills_state_root(drive_root) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_skill_name(name: str) -> str:
    """Clamp a skill name to a safe on-disk identifier.

    Keep alphanumerics, dashes, underscores, and dots; replace everything
    else with ``_``. Empty / pathological inputs become ``"_unnamed"``.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or "").strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        return "_unnamed"
    return cleaned[:64]  # also bound length to keep state paths sane


def is_self_authored_skill_dir(
    skill_dir: pathlib.Path,
    *,
    drive_root: pathlib.Path | None = None,
) -> bool:
    """Return True when a skill carries the agent-authored provenance marker."""
    skill_dir = pathlib.Path(skill_dir)
    marker = skill_dir / SELF_AUTHORED_MARKER_FILENAME
    data = read_json_dict(marker)
    if data is None:
        return False
    try:
        payload_schema = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    if not (isinstance(data, dict) and payload_schema == 1 and str(data.get("origin") or "") == "self_authored"):
        return False
    try:
        if drive_root is None:
            from ouroboros.app.config import DATA_DIR

            drive_root = pathlib.Path(DATA_DIR)
        state_marker = (
            pathlib.Path(drive_root) / "state" / "skills" / _sanitize_skill_name(skill_dir.name) / "self_authored.json"
        )
        state_data = read_json_dict(state_marker)
    except OSError:
        return False
    if state_data is None:
        return False
    try:
        state_schema = int(state_data.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(state_data, dict)
        and state_schema == 1
        and str(state_data.get("origin") or "") == "self_authored"
        and str(state_data.get("task_id") or "") == str(data.get("task_id") or "")
        and str(state_data.get("created_at") or "") == str(data.get("created_at") or "")
    )


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


def _iter_payload_files(
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
            resolved.relative_to(resolved_root)
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
            if any(part in _SKILL_DIR_CACHE_NAMES for part in rel_parts):
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
                real.relative_to(resolved_root)
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
    for file_path in _iter_payload_files(
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


# State persistence


def load_enabled(drive_root: pathlib.Path, name: str) -> bool:
    state = read_json_dict(skill_state_dir(drive_root, name) / "enabled.json")
    if not isinstance(state, dict):
        return False
    enabled = state.get("enabled")
    return enabled if isinstance(enabled, bool) else False


def save_enabled(drive_root: pathlib.Path, name: str, enabled: bool) -> None:
    atomic_write_json(
        skill_state_dir(drive_root, name) / "enabled.json",
        {
            "enabled": bool(enabled),
            "updated_at": utc_now_iso(),
        },
    )


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


_GRANTABLE_SKILL_PERMISSIONS = frozenset(
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
            grant = f"subscribe_event:{topic}"
            if grant in _GRANTABLE_SKILL_PERMISSIONS and grant not in requested:
                requested.append(grant)
    return sorted(requested)


def load_skill_grants(drive_root: pathlib.Path, name: str) -> dict[str, Any]:
    data = read_json_dict(skill_state_dir(drive_root, name) / GRANTS_FILENAME)
    if not isinstance(data, dict):
        return {"granted_keys": [], "granted_permissions": [], "updated_at": ""}
    keys = []
    for raw_key in data.get("granted_keys") or []:
        key = str(raw_key or "").strip().upper()
        if key and key not in keys:
            keys.append(key)
    requested = []
    for raw_key in data.get("requested_keys") or []:
        key = str(raw_key or "").strip().upper()
        if key and key not in requested:
            requested.append(key)
    permissions = []
    for raw_permission in data.get("granted_permissions") or []:
        permission = str(raw_permission or "").strip()
        if permission and permission not in permissions:
            permissions.append(permission)
    requested_permissions = []
    for raw_permission in data.get("requested_permissions") or []:
        permission = str(raw_permission or "").strip()
        if permission and permission not in requested_permissions:
            requested_permissions.append(permission)
    return {
        "granted_keys": keys,
        "requested_keys": requested,
        "granted_permissions": permissions,
        "requested_permissions": requested_permissions,
        "content_hash": str(data.get("content_hash") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_skill_grants(
    drive_root: pathlib.Path,
    name: str,
    granted_keys: list[str],
    *,
    content_hash: str,
    requested_keys: list[str],
    granted_permissions: list[str] | None = None,
    requested_permissions: list[str] | None = None,
) -> None:
    """Persist grants, merging partial approvals only for the same request hash."""
    allowed = set(requested_core_setting_keys(requested_keys))
    existing = load_skill_grants(drive_root, name)
    persisted_match = str(existing.get("content_hash") or "") == str(content_hash or "") and sorted(
        existing.get("requested_keys") or []
    ) == sorted(allowed)
    merged: list[str] = []
    if persisted_match:
        for raw_key in existing.get("granted_keys") or []:
            key = str(raw_key or "").strip().upper()
            if key and key in allowed and key not in merged:
                merged.append(key)
    for raw_key in granted_keys or []:
        key = str(raw_key or "").strip().upper()
        if key and key in allowed and key not in merged:
            merged.append(key)
    allowed_permissions = set(requested_permissions or [])
    existing_permissions: list[str] = []
    if persisted_match:
        for raw_permission in existing.get("granted_permissions") or []:
            permission = str(raw_permission or "").strip()
            if permission and permission in allowed_permissions and permission not in existing_permissions:
                existing_permissions.append(permission)
    for raw_permission in granted_permissions or []:
        permission = str(raw_permission or "").strip()
        if permission and permission in allowed_permissions and permission not in existing_permissions:
            existing_permissions.append(permission)
    atomic_write_json(
        skill_state_dir(drive_root, name) / GRANTS_FILENAME,
        {
            "granted_keys": merged,
            "requested_keys": sorted(allowed),
            "granted_permissions": sorted(existing_permissions),
            "requested_permissions": sorted(allowed_permissions),
            "content_hash": str(content_hash or ""),
            "updated_at": utc_now_iso(),
        },
    )


def grant_status_for_skill(drive_root: pathlib.Path, skill: LoadedSkill) -> dict[str, Any]:
    requested = requested_core_setting_keys(list(skill.manifest.env_from_settings or []))
    requested_permissions = requested_skill_permissions(
        list(skill.manifest.permissions or []),
        list(getattr(skill.manifest, "subscribe_events", []) or []),
    )
    if skill.distribution_owned:
        eligible_type = skill.manifest.is_script() or skill.manifest.is_extension() or skill.manifest.is_tool()
        unsupported = bool((requested or requested_permissions) and not eligible_type)
        return {
            "requested_keys": requested,
            "granted_keys": list(requested),
            "missing_keys": [],
            "requested_permissions": requested_permissions,
            "granted_permissions": list(requested_permissions),
            "missing_permissions": [],
            "all_granted": not unsupported,
            "usable": not unsupported,
            "unsupported_for_skill_type": unsupported,
            "content_hash": skill.content_hash,
            "updated_at": "distribution",
        }
    grants = load_skill_grants(drive_root, skill.name)
    grant_hash_ok = str(grants.get("content_hash") or "") == str(skill.content_hash or "")
    grant_request_ok = sorted(grants.get("requested_keys") or []) == sorted(requested)
    permission_request_ok = sorted(grants.get("requested_permissions") or []) == sorted(requested_permissions)
    persisted_grants = set(grants.get("granted_keys") or []) if grant_hash_ok and grant_request_ok else set()
    persisted_permissions = (
        set(grants.get("granted_permissions") or []) if grant_hash_ok and permission_request_ok else set()
    )
    granted = [key for key in requested if key in persisted_grants]
    granted_permissions = [perm for perm in requested_permissions if perm in persisted_permissions]
    missing = [key for key in requested if key not in set(granted)]
    missing_permissions = [perm for perm in requested_permissions if perm not in set(granted_permissions)]
    # Scripts receive core keys via _scrub_env; extensions via PluginAPI.
    # Instruction skills cannot receive core keys.
    eligible_type = skill.manifest.is_script() or skill.manifest.is_extension() or skill.manifest.is_tool()
    unsupported = bool((requested or requested_permissions) and not eligible_type)
    return {
        "requested_keys": requested,
        "granted_keys": granted,
        "missing_keys": missing,
        "requested_permissions": requested_permissions,
        "granted_permissions": granted_permissions,
        "missing_permissions": missing_permissions,
        "all_granted": not missing and not missing_permissions and not unsupported,
        "usable": not missing and not missing_permissions and not unsupported,
        "unsupported_for_skill_type": unsupported,
        "content_hash": grants.get("content_hash", ""),
        "updated_at": grants.get("updated_at", ""),
    }


# Discovery / loading


def _safe_listdir(root: pathlib.Path) -> list[pathlib.Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        log.warning("Failed to list skills repo %s", root, exc_info=True)
        return []


def load_skill(
    skill_dir: pathlib.Path,
    drive_root: pathlib.Path,
    *,
    source_root: pathlib.Path | None = None,
    source_kind: str = "",
    precedence: int = 0,
) -> LoadedSkill | None:
    """Load one skill, returning ``None`` only when no manifest exists."""
    skill_dir = skill_dir.resolve()
    try:
        manifest_read = _manifest_text_for_dir(skill_dir)
    except _ManifestUnreadable as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
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

    # Runtime/state/tool identity is the directory basename. manifest.name is
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
    enabled = source_kind == "distribution" or load_enabled(drive_root, name)

    return LoadedSkill(
        name=name,
        skill_dir=skill_dir,
        manifest=manifest,
        content_hash=content_hash,
        enabled=enabled,
        load_error=load_error,
        is_self_authored=is_self_authored_skill_dir(skill_dir, drive_root=drive_root),
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


def _coerce_str_list_setting(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item or "").strip()]
    return None


def _coerce_dict_list_setting(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, dict)]
    return []


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


def _resolve_openclaw_roots(
    workspace_root: pathlib.Path | None,
    settings: Any,
    *,
    drive_root: pathlib.Path | None = None,
    repo_path: str | None = None,
) -> list[tuple[str, pathlib.Path]]:
    del settings, drive_root
    repo_text = str(repo_path or "").strip()
    if repo_text:
        roots = [("distribution", pathlib.Path(repo_text).expanduser().resolve(strict=False))]
    else:
        try:
            from ouroboros.app.config import REPO_DIR

            repo_dir = pathlib.Path(REPO_DIR)
        except Exception:
            repo_dir = pathlib.Path.cwd()
        workspace = pathlib.Path(workspace_root or repo_dir).resolve(strict=False)
        roots = [("distribution", workspace / "skills")]

    # The tree that ships with the running code, last so a copy under the configured root
    # still wins. Skipped when the configured root already is it, which is the ordinary
    # single-checkout deployment.
    bundled = bundled_skills_root()
    if bundled is not None and not any(_is_bundled_root(root) for _, root in roots):
        roots.append((BUNDLED_ROOT_KIND, bundled))
    return roots


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


def _discover_openclaw_skill_dirs(
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
            dirnames[:] = [name for name in dirnames if not name.startswith(".") and name not in _SKILL_DIR_CACHE_NAMES]
        else:
            dirnames[:] = [name for name in dirnames if name not in _SKILL_DIR_CACHE_NAMES]
        if _OPENCLAW_MANIFEST_NAME not in filenames:
            continue
        skill_path = current / _OPENCLAW_MANIFEST_NAME
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


def _effective_allowlist(settings: Any, agent_name: str | None) -> list[str] | None:
    agents = _coerce_dict_list_setting(_settings_get(settings, "agents.list"))
    if agent_name:
        for item in agents:
            if str(item.get("name") or "").strip() == agent_name:
                return _coerce_str_list_setting(item.get("skills"))
    return _coerce_str_list_setting(_settings_get(settings, "agents.defaults.skills"))


def _matches_allowlist(skill: LoadedSkill, allowlist: list[str] | None) -> bool:
    if allowlist is None:
        return True
    allowed = {str(item).strip() for item in allowlist if str(item or "").strip()}
    if not allowed:
        return False
    return skill.name in allowed or skill.skill_key in allowed


def _skill_tree_stats(skill_dir: pathlib.Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    try:
        files = _iter_payload_files(skill_dir)
    except SkillPayloadUnreadable:
        return 0, 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
            file_count += 1
        except OSError:
            continue
    return file_count, total_bytes


def _openclaw_frontmatter_error(skill: LoadedSkill) -> str:
    path = skill.skill_path or (skill.skill_dir / _OPENCLAW_MANIFEST_NAME)
    if path.name != _OPENCLAW_MANIFEST_NAME:
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


def _apply_openclaw_validation(
    skill: LoadedSkill,
    settings: Any,
    allowlist: list[str] | None,
) -> None:
    reasons: list[str] = []
    manifest_path = skill.skill_path or (skill.skill_dir / _OPENCLAW_MANIFEST_NAME)
    if manifest_path.is_symlink():
        reasons.append("skill manifest must be a regular file, not a symlink")
    frontmatter_error = _openclaw_frontmatter_error(skill)
    if frontmatter_error:
        reasons.append(frontmatter_error)
        if not skill.load_error:
            skill.load_error = frontmatter_error
    if not skill.manifest.description:
        reasons.append("missing required frontmatter field: description")
    if skill.load_error and skill.load_error not in reasons:
        reasons.append(skill.load_error)

    openclaw = _openclaw_metadata(skill.manifest)
    raw_requires = openclaw.get("requires")
    requires: dict[str, Any] = raw_requires if isinstance(raw_requires, dict) else {}
    allowed_os = _normalize_list(openclaw.get("os"))
    if allowed_os and not (_current_os_tokens() & {item.lower() for item in allowed_os}):
        reasons.append(f"os gate failed: requires one of {allowed_os}")
    for binary in _normalize_list(requires.get("bins")):
        if shutil.which(binary) is None:
            reasons.append(f"missing required bin: {binary}")
    for key in _normalize_list(requires.get("env")):
        from ouroboros.storage.settings.store import load_settings

        if not str(load_settings().get(key, "") or "").strip():
            reasons.append(f"missing required env: {key}")
    for key in _normalize_list(requires.get("config")):
        if not _setting_path_value(settings, key):
            reasons.append(f"missing required config: {key}")

    file_count, total_bytes = _skill_tree_stats(skill.skill_dir)
    if file_count > _MAX_SKILL_FILE_COUNT:
        reasons.append(f"oversized skill tree: {file_count} files exceeds {_MAX_SKILL_FILE_COUNT}")
    if total_bytes > _MAX_SKILL_TREE_BYTES:
        reasons.append(f"oversized skill tree: {total_bytes} bytes exceeds {_MAX_SKILL_TREE_BYTES}")
    try:
        skill_bytes = pathlib.Path(skill.location).stat().st_size
    except OSError:
        skill_bytes = 0
    if skill_bytes > _MAX_SKILL_FILE_BYTES:
        reasons.append(f"oversized SKILL.md: {skill_bytes} bytes exceeds {_MAX_SKILL_FILE_BYTES}")

    if not _matches_allowlist(skill, allowlist):
        reasons.append("agent allowlist blocks this skill")
    if skill.shadowed_by:
        reasons.append(f"shadowed by higher-precedence copy at {skill.shadowed_by}")
    if skill.manifest.is_tool():
        entry = str(skill.manifest.entry or "tool.py").strip() or "tool.py"
        entry_path = pathlib.PurePosixPath(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts:
            reasons.append("type=tool entry must stay inside the skill directory")
        elif not (skill.skill_dir / entry).is_file():
            reasons.append(f"type=tool requires entry file {entry}")

    skill.blocked_reasons = list(dict.fromkeys(reasons))
    skill.eligible = skill.selected and not skill.blocked_reasons


def _discover_openclaw_skills(
    drive_root: pathlib.Path,
    *,
    workspace_root: pathlib.Path | None,
    agent_name: str | None,
    repo_path: str | None = None,
    settings: AppSettings | None = None,
) -> list[LoadedSkill]:
    settings = load_settings() if settings is None else settings
    allowlist = _effective_allowlist(settings, agent_name)
    discovered: list[LoadedSkill] = []

    roots = _resolve_openclaw_roots(
        workspace_root,
        settings,
        drive_root=pathlib.Path(drive_root),
        repo_path=repo_path,
    )
    hidden_bundled = inactive_bundled_skill_names(settings)
    for precedence, (root_kind, root) in enumerate(roots):
        # The shipped tree is a root like any other once it is reached, except that only the
        # registered packs travel with the code — the rest of that directory belongs to
        # whoever configured it, not to every deployment this code runs in.
        from_pack = root_kind == BUNDLED_ROOT_KIND
        source_kind = "distribution" if from_pack else root_kind
        skill_dirs, skipped = _discover_openclaw_skill_dirs(root)
        for item in skipped:
            name = _sanitize_skill_name(item["name"])
            discovered.append(
                LoadedSkill(
                    name=name,
                    skill_dir=pathlib.Path(item["location"]).parent,
                    manifest=SkillManifest(name=name, description="", version="", type="instruction"),
                    content_hash="",
                    load_error=item["reason"],
                    source=source_kind,
                    skill_path=pathlib.Path(item["location"]),
                    source_root=root,
                    source_kind=source_kind,
                    precedence=precedence,
                    selected=False,
                    eligible=False,
                    blocked_reasons=[item["reason"]],
                )
            )
        for skill_dir in skill_dirs:
            loaded = load_skill(
                skill_dir,
                pathlib.Path(drive_root),
                source_root=root,
                source_kind=source_kind,
                precedence=precedence,
            )
            if loaded is None:
                continue
            if _openclaw_frontmatter_error(loaded):
                loaded.name = _sanitize_skill_name(skill_dir.name)
            else:
                loaded.name = _sanitize_skill_name(loaded.manifest.name or skill_dir.name)
            if from_pack and loaded.name not in BUNDLED_SKILL_NAMES:
                continue
            if loaded.name in hidden_bundled and (from_pack or _is_bundled_root(root)):
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
                loaded.enabled = True
            discovered.append(loaded)

    same_precedence: dict[tuple[str, int], list[LoadedSkill]] = {}
    for skill in discovered:
        same_precedence.setdefault((skill.name, skill.precedence), []).append(skill)
    collision_keys = {key for key, group in same_precedence.items() if len(group) > 1}
    for key in collision_keys:
        group = same_precedence[key]
        locations = ", ".join(skill.location for skill in group)
        for skill in group:
            skill.selected = False
            skill.load_error = (
                f"Skill name collision: multiple skill directories at the same "
                f"precedence ({locations}) resolve to {skill.name!r}."
            )

    winners: dict[str, LoadedSkill] = {}
    for skill in sorted(discovered, key=lambda item: (item.precedence, item.location)):
        if (skill.name, skill.precedence) in collision_keys:
            continue
        winners.setdefault(skill.name, skill)
    for skill in discovered:
        winner = winners.get(skill.name)
        if winner is not None and winner.location != skill.location:
            skill.selected = False
            skill.shadowed_by = winner.location
        _apply_openclaw_validation(skill, settings, allowlist)

    discovered.sort(key=lambda item: (item.name, item.precedence, item.location))
    return discovered


def discover_skills(
    drive_root: pathlib.Path,
    repo_path: str | None = None,
    *,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
) -> list[LoadedSkill]:
    """Discover trusted distribution skills from ``repo/skills`` only."""
    return _discover_openclaw_skills(
        pathlib.Path(drive_root),
        workspace_root=workspace_root,
        agent_name=agent_name,
        repo_path=repo_path,
    )


def find_skill(
    drive_root: pathlib.Path,
    name: str,
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
) -> LoadedSkill | None:
    """Return one skill by name, including broken manifests with ``load_error``."""
    safe = _sanitize_skill_name(name)
    for skill in discover_skills(
        drive_root,
        repo_path=repo_path,
        workspace_root=workspace_root,
        agent_name=agent_name,
    ):
        if skill.name == safe:
            return skill
    return None


def list_available_for_execution(
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
) -> list[LoadedSkill]:
    """Return only skills that are enabled and ready for execution."""
    return [
        s
        for s in discover_skills(drive_root, repo_path=repo_path, workspace_root=workspace_root)
        if s.available_for_execution and grant_status_for_skill(drive_root, s).get("usable", True)
    ]


# Status helpers consumed by the Skills UI


class SkillToolSurface(TypedDict):
    name: str
    description: str


class SkillSummaryRow(TypedDict):
    name: str
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
    enabled: bool
    available_for_execution: bool
    runnable_via_skill_exec: bool
    tool_surfaces: list[SkillToolSurface]
    static_ready: bool
    blocked_by_grants: bool
    load_error: str
    source: str


class SkillSummary(TypedDict):
    count: int
    selected: int
    eligible: int
    available: int
    blocked_by_grants: int
    broken: int
    skills: list[SkillSummaryRow]


class SkillCheckSummary(SkillSummary):
    invalid: int


def summarize_skills(
    drive_root: pathlib.Path,
    *,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
) -> SkillSummary:
    """Return a compact catalogue summary for the Skills UI / /api/state."""
    skills = discover_skills(drive_root, workspace_root=workspace_root, agent_name=agent_name)
    tool_surfaces_by_skill: dict[str, list[SkillToolSurface]] = {}
    extension_states: dict[str, dict[str, Any]] = {}
    try:
        from ouroboros.skills.extensions.loader import registered_tools, runtime_state_for_loaded_skill

        for tool in registered_tools():
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
            skill.name: runtime_state_for_loaded_skill(skill, pathlib.Path(drive_root))
            for skill in skills
            if skill.manifest.is_extension()
        }
    except Exception:
        tool_surfaces_by_skill = {}
        extension_states = {}

    rows: list[SkillSummaryRow] = []
    for skill in skills:
        grants = grant_status_for_skill(drive_root, skill)
        grant_usable = bool(grants.get("usable", True))
        extension_state = extension_states.get(skill.name, {})
        live_loaded = bool(extension_state.get("live_loaded"))
        blocked_reason = ""
        if skill.load_error:
            blocked_reason = skill.load_error
        elif not skill.selected or not skill.eligible:
            blocked_reason = next(iter(skill.blocked_reasons), "not_eligible")
        elif not skill.enabled:
            blocked_reason = "disabled"
        elif not grant_usable:
            blocked_reason = "missing_grants"
        elif skill.manifest.is_extension() and not live_loaded:
            blocked_reason = str(extension_state.get("load_error") or "not_live_loaded")

        if skill.manifest.is_extension():
            ready = live_loaded and not blocked_reason
        elif skill.manifest.is_instruction():
            ready = skill.selected and skill.eligible and skill.enabled and not blocked_reason
        else:
            ready = skill.available_for_execution and grant_usable and not blocked_reason

        rows.append(
            {
                "name": skill.name,
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
                "enabled": skill.enabled,
                "available_for_execution": skill.available_for_execution and grant_usable,
                "runnable_via_skill_exec": skill.available_for_execution,
                "tool_surfaces": tool_surfaces_by_skill.get(skill.name, []),
                "static_ready": skill.available_for_execution,
                "blocked_by_grants": not grant_usable,
                "load_error": skill.load_error,
                "source": skill.source,
            }
        )
    return {
        "count": len(skills),
        "selected": sum(1 for s in skills if s.selected),
        "eligible": sum(1 for s in skills if s.eligible),
        "available": sum(1 for row in rows if row["ready"]),
        "blocked_by_grants": sum(1 for row in rows if row["blocked_by_grants"]),
        "broken": sum(1 for s in skills if s.load_error),
        "skills": rows,
    }


def check_skills(
    drive_root: pathlib.Path,
    *,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
) -> SkillCheckSummary:
    """Return deterministic discovery plus validation failures."""
    summary = summarize_skills(
        drive_root,
        workspace_root=workspace_root,
        agent_name=agent_name,
    )
    return {
        **summary,
        "invalid": sum(1 for skill in summary["skills"] if not skill.get("eligible")),
    }


__all__ = [
    "LoadedSkill",
    "SkillCheckSummary",
    "SkillSummary",
    "SkillSummaryRow",
    "SkillToolSurface",
    "check_skills",
    "compute_content_hash",
    "discover_skills",
    "find_skill",
    "grant_status_for_skill",
    "is_self_authored_skill_dir",
    "list_available_for_execution",
    "load_enabled",
    "load_skill",
    "load_skill_grants",
    "requested_core_setting_keys",
    "save_enabled",
    "save_skill_grants",
    "skill_state_dir",
    "summarize_skills",
]
