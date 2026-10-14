---
name: test-instruction
description: "Test fixture for type=instruction skills. Contributes prompt text only — no tools, no scripts, no code. Use when a test asks to verify instruction-skill discovery, or asks for the instruction skill marker."
version: 1.0.0
type: instruction
when_to_use: "A test verifies that type=instruction skills are discovered, pass validation, and reach the model as prompt text."
---

# Test Instruction Skill

This skill exists to prove one thing: a `type: instruction` skill contributes
**prompt text and nothing else**. It registers no tools and ships no executable
files, so `list_skills` must report it as discovered but not executable.

## Assertion marker

When asked for the instruction skill marker, reply with exactly:

```
OUROBOROS_TEST_INSTRUCTION_OK
```

A test can grep the model's answer for that token to confirm this body was
loaded into the prompt.

## What this skill must NOT be able to do

- It has no `scripts:` array, so `skill_exec` must refuse it with a
  "not a declared script" style error.
- It has no `entry`, so no tool alias may appear for it.
- Because `env_from_settings` is empty, no settings key is forwarded anywhere.

If any of the above stops holding, instruction-skill isolation has regressed.
