---
paths:
  - "new_core/skills/**"
  - "ouroboros/skills/**"
  - "ouroboros/tools/skill_*.py"
  - "skills/**"
---

# Skills and extensions — read before editing

| Concern | Owner |
|---|---|
| Stable source identity | `new_core/skills/models.py` |
| Source contracts and DI distribution provider | `new_core/skills/interfaces.py`, `new_core/skills/providers/distribution.py` |
| Process-wide package union | `new_core/skills/catalog.py` |
| Deployment/run availability projection | `new_core/skills/surface.py` |
| Skill transport permission decision | `new_core/skills/permissions.py` |
| Distribution filesystem and application policy | `ouroboros/skills/loader.py`, `ouroboros/skills/bundled.py`, `ouroboros/skills/availability.py` |
| Extension lifecycle and companions | `ouroboros/skills/extensions/` |
| Subprocess execution and sandboxing | `ouroboros/tools/skill_exec.py`, `ouroboros/sandbox/` |
| Payloads (also a pytest testpath) | `skills/<name>/` |

**Rules:** payloads live only under the trusted repository `skills/<name>/`; runtime code must not seed or scan data/skills buckets. Treat the production repository as read-only — payload changes go through the release repository. Distribution manifests are the capability/settings allowlist; extension load stays fail-closed, and live readiness is reported separately from discovery. Stage in-process extension imports under `/tmp`; durable `data/state/skills/<name>/` is for private extension state only.

Providers own source-global discovery and validation, never owner or agent projection; `SkillSurface` owns the legacy agent allowlist, the common multi-user executable cutoff, and a task selection that is `None` only for an unbound deployment surface. An empty task selection denies all. A run receives one immutable catalog snapshot and one frozen owner-and-agent selection. `new_core/skills` imports neither `ouroboros` nor `transport`. There is no runtime repair, enablement, grant, or dependency-installation lifecycle.
