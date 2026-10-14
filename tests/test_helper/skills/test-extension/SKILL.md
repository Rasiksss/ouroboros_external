---
name: test-extension
description: "Test fixture for type=extension skills. Its plugin.py register(api) hook registers ext_ping, ext_state, and ext_settings through PluginAPI. Use when a test asks to call the test extension skill."
version: 1.0.0
type: extension
runtime: python3
entry: plugin.py
permissions: [tool, fs, read_settings]
env_from_settings:
  - OUROBOROS_TEST_SKILL_TOKEN
when_to_use: "A test verifies that type=extension skills load through PluginAPI, register tools under the provider-safe surface name, and get a writable state dir plus allowlisted settings."
---

# Test Extension Skill

`plugin.py` exports `register(api)`. The loader imports it, builds a `PluginAPI`
bound to this skill's permissions, and calls `register` once. The extension then
registers three tools:

- `ext_ping` — returns a fixed marker
- `ext_state` — writes and reads back a file in the skill state dir (needs `fs`)
- `ext_settings` — reports which allowlisted settings arrived (needs `read_settings`)

## Naming

Extension tools are not exposed under their short names. They get a
provider-safe surface name built from the skill token, roughly
`ext_<len>_r_test-extension_<short>`. Tests should resolve them through
`resolve_extension_tool_name`, which also accepts the `test-extension.ext_ping`
alias spelling, rather than hardcoding the mangled form.

## Permission gates

`permissions` is the complete trusted capability surface. This fixture asks only for `tool`,
`fs`, and `read_settings`, so any PluginAPI call outside that set must raise
rather than silently succeed — that negative case is what makes this fixture
useful. It deliberately does **not** request `net`, `subprocess`,
`companion_process`, `inject_chat`, or `send_message`.

Under a distribution root those declarations are trusted when the snapshot is published.
