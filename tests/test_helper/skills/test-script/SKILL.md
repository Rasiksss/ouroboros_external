---
name: test-script
description: "Test fixture for type=script skills. Ships three declared scripts (echo_args, fail, slow) that exercise the skill_exec happy path, non-zero exit handling, and timeout handling. Use when a test asks to run the script skill."
version: 1.0.0
type: script
runtime: python3
timeout_sec: 10
env_from_settings:
  - OUROBOROS_TEST_SKILL_TOKEN
scripts:
  - name: echo_args.py
    description: "Print a JSON report of argv, cwd, and the scrubbed environment."
  - name: fail.py
    description: "Exit with a non-zero status and write to stderr."
  - name: slow.py
    description: "Sleep longer than timeout_sec to exercise the timeout path."
when_to_use: "A test verifies that type=script skills execute through skill_exec with a scrubbed env, cwd confinement, and bounded timeout."
---

# Test Script Skill

Executable surface is exactly the manifest `scripts:` array. Bare names resolve
under `scripts/`, so `echo_args.py` and `scripts/echo_args.py` are both accepted
spellings, while an undeclared file in the same directory must be rejected.

## Calls

```
skill_exec(skill="test-script", script="echo_args.py", args=["alpha", "beta"])
skill_exec(skill="test-script", script="fail.py")
skill_exec(skill="test-script", script="slow.py")
```

## What each script proves

| Script | Expectation |
| --- | --- |
| `echo_args.py` | exit 0, stdout is one JSON object with the argv that was passed |
| `fail.py` | exit 3, stderr text surfaces in the tool result |
| `slow.py` | killed at `timeout_sec` (10s), reported as a timeout, not a hang |

## Env forwarding

`env_from_settings` requests `OUROBOROS_TEST_SKILL_TOKEN`. Set it in your `.env`
and `echo_args.py` echoes it back under `env.OUROBOROS_TEST_SKILL_TOKEN`. The
key needs the `OUROBOROS_` prefix — settings only absorb unknown env keys whose
name starts with one of the allowlisted prefixes, so a bare `TEST_SKILL_TOKEN`
would silently never arrive.

Undeclared core secrets are refused: `echo_args.py` also reports
`leaked_core_keys`, which must stay empty.

`OUROBOROS_SKILL_NAME` and `OUROBOROS_SKILL_STATE_DIR` are always injected by
the executor, so they should be present without being declared.

## Negative case

There is an undeclared file at `scripts/undeclared.py`. Asking for it must fail:

```
skill_exec(skill="test-script", script="scripts/undeclared.py")
```
