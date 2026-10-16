## Why

With `OUROBOROS_MULTIUSER=true` the common skill surface withholds every executable skill (`script`,
`extension`, `tool`) from the whole process, and nothing a deployment can set widens that. A deployment that
ships its own trusted `extension` in the repository's `skills/` tree — the AIFA symbiosis deployment ships
`aifa-engine`, whose tools call its own service over the internal network — therefore loads no tools in the mode
that identifies its users by header. The ceiling is deliberate and stays closed by default; the deployment needs
one named way to open it for exactly the distribution skills it trusts.

## What Changes

- New deployment setting `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS`: a list of canonical skill ids, empty by
  default. Every entry MUST be a `distribution:<skill>` id; any other source is refused at settings load.
- The common skill surface keeps the ceiling and exempts exactly the named ids: an executable skill is visible
  under the mode only if its canonical id is in that list. Documentation skills are unaffected.
- The owner-and-agent allowlist `OUROBOROS_USER_ENABLED_SKILL_IDS` still applies below the ceiling: a trusted
  executable skill reaches a task only when that allowlist also names it.
- `skill_exec` stays unregistered under the mode; no execution path for `script` or `tool` skills is added.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `multiuser-mode`: the requirement that no skill that executes code is exposed, and the sentence of the
  skill-execution requirement that every executable package is withheld, gain the named-trust exemption.

## Impact

- `ouroboros/storage/settings/schema.py` (field and validator), `new_core/skills/surface.py`
  (`SkillAvailability`, `SkillSurface._is_visible`), `ouroboros/skills/availability.py` (pass the value),
  `docs/SETTINGS.md`.
- Behaviour with the setting empty is identical to today. Outside the mode nothing changes.
- Extension loading exists only in the single-process composition root; the browser-transport and agent tiers
  still add none, so a trusted extension is loaded where that root runs.
