"""``type: tool`` skills: a skill's ``tool.py`` exposed under a ``<skill>.<tool>`` alias."""

from __future__ import annotations

import importlib.util
import logging
import pathlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.providers.builtin import descriptor_from_entry, entry_from_decl
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.tools.catalog import (
    DECLARABLE_CAPABILITY_CLASSES,
    DECLARABLE_SAFETY_POLICIES,
    RESULT_LIMIT_HARD_MAX,
)

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext

log = logging.getLogger(__name__)


def _namespace(skill_name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(skill_name or "").strip()).strip("_")
    return safe or "skill"


def _normalized_choice(alias: str, field: str, value: object, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    candidate = str(value or "").strip().lower()
    if candidate not in allowed:
        raise ValueError(f"tool {alias!r} {field} must be one of {sorted(allowed)}")
    return candidate


def _validated_entry_metadata(
    alias: str, entry: ToolEntry
) -> tuple[str | None, str | None, bool | None, tuple[str, ...]]:
    """Normalize the declared metadata, rejecting what register_tool rejects (#84).

    A bad declaration raises and the skill loads no tools. Returns the values
    to carry across namespacing: (safety_policy, capability_class,
    parallel_safe, retrieval_aliases).
    """
    safety = _normalized_choice(alias, "safety_policy", entry.safety_policy, DECLARABLE_SAFETY_POLICIES)
    capability = _normalized_choice(alias, "capability_class", entry.capability_class, DECLARABLE_CAPABILITY_CLASSES)

    if not isinstance(entry.retrieval_aliases, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in entry.retrieval_aliases
    ):
        raise ValueError(f"tool {alias!r} retrieval_aliases must be a list/tuple of non-empty strings")
    aliases = tuple(item.strip() for item in entry.retrieval_aliases)

    limit = entry.result_limit
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > RESULT_LIMIT_HARD_MAX
    ):
        raise ValueError(f"tool {alias!r} result_limit must be a positive int <= {RESULT_LIMIT_HARD_MAX} or None")

    properties = entry.schema.parameters.get("properties")
    known = set(properties) if isinstance(properties, dict) else set()
    unknown = sorted(set(entry.log_redacted_args) - known)
    if unknown:
        raise ValueError(f"tool {alias!r} log_redacted_args not in schema properties: {', '.join(unknown)}")

    parallel_safe = entry.parallel_safe
    if parallel_safe is not None and not isinstance(parallel_safe, bool):
        # A string like "false" is truthy; bool(descriptor.parallel_safe) would
        # put the tool in a parallel batch against its own declaration.
        raise ValueError(f"tool {alias!r} parallel_safe must be a bool")
    if parallel_safe and safety != "skip":
        # Same demotion as the extension loader: a batch of "check" tools would
        # fire that many concurrent safety-LLM calls per round, but the flag
        # predates safety_policy, so an existing declaration keeps loading.
        log.warning(
            "tool %r declares parallel_safe without safety_policy='skip'; registering as not parallel-safe", alias
        )
        parallel_safe = False
    return safety, capability, parallel_safe, aliases


class SkillToolProvider(IToolProvider):
    """Tools from enabled, reviewed ``type: tool`` skills under one drive root.

    The discovery, grant, and content-hash gates are the ones the registry applied: a skill
    contributes tools only while it is enabled, eligible, error-free, and its recorded grant
    hash still matches its content hash.
    """

    default_runtime = THREAD_RUNTIME

    def __init__(self, drive_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        self._drive_root = pathlib.Path(drive_root)
        self._repo_dir = pathlib.Path(repo_dir)
        self._entries: dict[str, ToolEntry] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}

    def refresh(self) -> None:
        from ouroboros.skills.loader import discover_skills, grant_status_for_skill

        entries: dict[str, ToolEntry] = {}
        descriptors: dict[str, ToolDescriptor] = {}
        try:
            skills = discover_skills(self._drive_root, workspace_root=self._repo_dir)
        except Exception:
            log.debug("Failed to discover tool skills", exc_info=True)
            self._entries = entries
            self._descriptors = descriptors
            return

        for skill in skills:
            try:
                for alias, entry in self._entries_for_skill(skill, grant_status_for_skill):
                    entries[alias] = entry
                    # Skill tools are never promoted into the first prompt on their own.
                    descriptors[alias] = descriptor_from_entry(
                        entry,
                        f"skill:{skill.name}",
                    )
            except Exception:
                log.warning("Failed to load tool skill %s", getattr(skill, "name", "?"), exc_info=True)
        self._entries = entries
        self._descriptors = descriptors

    def _entries_for_skill(self, skill: Any, grant_status_for_skill: Any) -> list[tuple[str, ToolEntry]]:
        manifest = skill.manifest
        if not manifest.is_tool():
            return []
        if not skill.enabled or not skill.eligible or skill.load_error:
            return []
        grants = grant_status_for_skill(self._drive_root, skill)
        if str(grants.get("content_hash") or "") != str(skill.content_hash or ""):
            return []

        entry_rel = str(manifest.entry or "tool.py").strip() or "tool.py"
        skill_dir = pathlib.Path(skill.skill_dir).resolve(strict=False)
        entry_path = (pathlib.Path(skill.skill_dir) / entry_rel).resolve(strict=False)
        try:
            entry_path.relative_to(skill_dir)
        except ValueError:
            return []
        if not entry_path.is_file():
            return []

        spec = importlib.util.spec_from_file_location(
            f"_ouroboros_skill_tool_{skill.name.replace('-', '_').replace('.', '_')}",
            entry_path,
        )
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        get_tools = getattr(module, "get_tools", None)
        if not callable(get_tools):
            return []

        namespace = _namespace(skill.name)
        result: list[tuple[str, ToolEntry]] = []
        for decl in get_tools():
            entry = entry_from_decl(decl)
            original_name = str(entry.name or "").strip()
            if not original_name:
                continue
            alias = f"{namespace}.{original_name}"
            safety_policy, capability_class, parallel_safe, retrieval_aliases = _validated_entry_metadata(alias, entry)
            result.append(
                (
                    alias,
                    ToolEntry(
                        name=alias,
                        schema=ToolSchema(
                            name=alias,
                            description=entry.schema.description,
                            parameters=entry.schema.parameters,
                        ),
                        handler=entry.handler,
                        is_code_tool=entry.is_code_tool,
                        timeout_sec=entry.timeout_sec,
                        env_prefix=entry.env_prefix,
                        # Declared metadata survives namespacing; TOOL_SPECS cannot key an alias (#84).
                        safety_policy=safety_policy,
                        capability_class=capability_class,
                        result_limit=entry.result_limit,
                        parallel_safe=parallel_safe,
                        retrieval_aliases=retrieval_aliases,
                        log_redacted_args=entry.log_redacted_args,
                    ),
                )
            )
        return result

    def descriptors(self) -> Sequence[ToolDescriptor]:
        return list(self._descriptors.values())

    def get(self, name: str) -> ToolDescriptor | None:
        return self._descriptors.get(str(name or "").strip())

    def refuse_dispatch(self, name: str) -> str | None:
        """Nothing to withhold: the discovery gates run at ``refresh``, not per lookup.

        A skill that is disabled, ineligible or stale contributes no entries at all, so by
        the time a name reaches here this provider has no memory of ever owning it — unlike
        the extension provider, which reads its source live and can still name what it hides.
        """
        return None

    def entries(self) -> dict[str, ToolEntry]:
        """The local entries behind these descriptors, for the ``ToolRegistry`` façade."""
        return dict(self._entries)

    def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        entry = self._entries.get(str(name or "").strip())
        if entry is None:
            return f"⚠️ Unknown tool: {name}"
        try:
            return entry.handler(ctx, **args)
        except TypeError as exc:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {exc}"
        except Exception as exc:
            return f"⚠️ TOOL_ERROR ({name}): {exc}"
