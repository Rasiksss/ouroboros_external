## Context

`build_skill_availability` (`ouroboros/skills/availability.py`) turns `OUROBOROS_MULTIUSER` into
`SkillAvailability.executable_skills = False`. `SkillSurface._is_visible` (`new_core/skills/surface.py`) then
hides any skill whose manifest `is_executable()`. Everything downstream inherits this: the task selection in
`ouroboros/agent/task_graph.py` (`_build_task_skill_surface`), `admitted_skill_origins`
(`config_registry/user_settings/tool_access.py`) and the extension tool provider all read the same surface.

## Decisions

**Name the exemption, do not lift the ceiling.** A boolean "allow distribution executables" would silently admit
every future skill dropped into `skills/`. An explicit id list admits exactly what a deployment named, defaults
to empty, and is visible in the deployment description.

**One predicate, in the surface.** `_is_visible` becomes
`executable_skills or not is_executable() or str(skill_id) in trusted_executable_skills`. No provider-specific
omission or inclusion; the property stays "enforced in the common surface".

**`distribution:` only.** The distribution tree is the trusted, read-only payload shipped with the image.
Skills from any other source (a hub, a user) cannot be named; a malformed or foreign id fails settings
validation instead of being ignored, so a typo cannot widen or silently narrow the ceiling.

**The task-level narrowing is unchanged.** `_build_task_skill_surface` still intersects with
`OUROBOROS_USER_ENABLED_SKILL_IDS` when `executable_skills` is false. A trusted executable skill is therefore
doubly opt-in: deployment names it as trusted, the owner/agent allowlist (or instance default) enables it.

**No new execution path.** `skill_exec` remains unregistered under the mode, so `script` and `tool` skills named
as trusted become visible but have no runner; the only executable type that works under the mode is
`extension`, whose tools run in-process through the extension runtime of the single-process root.

## Risks

- A named extension runs in-process with the server's privileges. The list is the deployment's single point of
  trust; documented in `docs/SETTINGS.md`.
- Extensions cannot `send_message` under the mode (no author); a trusted extension must not depend on it.
