---
name: test-tool
description: "Test fixture for type=tool skills. Its tool.py exports get_tools() with three in-process tools (ping, echo, boom) that surface under the test_tool.* alias namespace. Use when a test asks to call the test tool skill."
version: 1.0.0
type: tool
runtime: python3
entry: tool.py
when_to_use: "A test verifies that SkillToolProvider discovers type=tool skills, namespaces their tools, and routes execution and errors through the registry."
---

# Test Tool Skill

`tool.py` exports `get_tools()` returning `ToolEntry` objects. `SkillToolProvider`
imports that module and re-exports each entry under a `<skill>.<tool>` alias, so
the names visible to the model are:

- `test_tool.ping`
- `test_tool.echo`
- `test_tool.boom`

The namespace comes from the skill name with non-alphanumerics folded to `_`, so
`test-tool` becomes `test_tool`.

## What each tool proves

| Alias | Expectation |
| --- | --- |
| `test_tool.ping` | returns a fixed marker string — proves the module loaded and routed |
| `test_tool.echo` | returns its arguments as JSON — proves kwargs reach the handler intact |
| `test_tool.boom` | raises — proves the provider wraps it as `⚠️ TOOL_ERROR (...)` rather than propagating |

Calling `test_tool.echo` with a bad argument name should come back as
`⚠️ TOOL_ARG_ERROR (...)`, since the provider converts `TypeError` separately.

## Discovery gates

These tools appear only while the skill is eligible and free of load errors.
The distribution manifest is the trust boundary. They are registered with
`default_enabled=False`, so
they never enter the first prompt on their own — a test must enable them
explicitly.
