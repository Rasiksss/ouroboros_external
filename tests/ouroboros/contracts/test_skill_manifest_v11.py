import pytest

from new_core.skills.manifest import parse_skill_manifest_text


def test_manifest_parses_companion_processes_and_subscribe_events() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: bridge
description: Bridge skill
version: 1.0.0
type: extension
entry: plugin.py
permissions: [companion_process, subscribe_event, inject_chat]
subscribe_events: [chat.outbound, chat.typing]
companion_processes:
  - name: daemon
    command: [python3, scripts/daemon.py]
    runtime: python3
---
# Bridge
"""
    )

    assert manifest.subscribe_events == ["chat.outbound", "chat.typing"]
    assert manifest.companion_processes[0]["name"] == "daemon"
    assert manifest.validate() == []


def test_manifest_warns_on_invalid_event_topic() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: bridge
description: Bridge skill
version: 1.0.0
type: extension
entry: plugin.py
subscribe_events: [Chat.Outbound]
---
# Bridge
"""
    )

    assert any("invalid subscribe_events topic" in warning for warning in manifest.validate())


@pytest.mark.parametrize(
    "command,error_match",
    [
        pytest.param("python3, /tmp/evil.py", "reviewed skill tree", id="absolute-script"),
        pytest.param("deno, run, /tmp/evil.ts", "reviewed skill tree", id="absolute-subcommand"),
        pytest.param("python3, -u, -c, \"print('x')\"", "inline/eval", id="inline-eval"),
    ],
)
def test_manifest_rejects_unreviewed_companion_commands(command: str, error_match: str) -> None:
    bad = f"""---
name: bad
description: Bad
version: 1.0.0
type: extension
entry: plugin.py
companion_processes:
  - name: daemon
    command: [{command}]
    runtime: python3
---
# Bad
"""

    with pytest.raises(Exception, match=error_match):
        parse_skill_manifest_text(bad)
