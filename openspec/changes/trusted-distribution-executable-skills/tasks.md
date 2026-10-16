## 1. Failing tests first

- [x] 1.1 `tests/new_core/skills/test_skill_surface.py`: with `executable_skills=False`, a named `distribution:` extension is visible and admitted when selected; an unnamed extension and a named id of another source stay hidden; the allowlist still narrows; the default is empty
- [x] 1.2 `tests/ouroboros/skills/`: `build_skill_availability` carries `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS`
- [x] 1.3 Settings: `distribution:<skill>` accepted, JSON list and comma list accepted, any other source, empty entry or non-list refused
- [x] 1.4 Run the new tests against the unchanged code and record that they fail

## 2. Setting and surface

- [x] 2.1 Declare `OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS` in `AppSettings` with the `distribution:` validator
- [x] 2.2 Add `trusted_executable_skills` to `SkillAvailability` and extend `_is_visible`
- [x] 2.3 Pass the setting in `build_skill_availability`

## 3. Documentation

- [x] 3.1 `docs/SETTINGS.md`: the setting, its default, and the narrowing by `OUROBOROS_USER_ENABLED_SKILL_IDS`
